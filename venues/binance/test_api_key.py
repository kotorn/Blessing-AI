"""
Binance API Key Diagnostic and Verification Tool
Tests public connectivity, time synchronization, authenticated account access,
API permissions/restrictions, and Hedge Mode configuration on Binance Global and Testnet.
"""

import os
import sys
import time
import hmac
import hashlib
import json
import urllib.request
import urllib.error
import urllib.parse
from typing import Dict, Any, Tuple, Optional


def sign_query(secret: str, params: Dict[str, Any]) -> str:
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(
        secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{query_string}&signature={signature}"


def http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 10) -> Tuple[int, Any]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content)
    except urllib.error.HTTPError as err:
        try:
            error_body = json.loads(err.read().decode("utf-8"))
        except Exception:
            error_body = {"raw": err.reason}
        return err.code, error_body
    except Exception as err:
        return 0, {"error": str(err)}


def test_binance_connectivity():
    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    is_testnet = os.getenv("BINANCE_TESTNET", "").lower() in ("true", "1", "yes")

    print("=================================================================")
    print("           BINANCE API KEY DIAGNOSTIC TEST RUNNER               ")
    print("=================================================================")

    if not api_key:
        print("[!] ERROR: BINANCE_API_KEY environment variable is missing or empty.")
        return
    if not api_secret:
        print("[!] ERROR: BINANCE_API_SECRET environment variable is missing or empty.")
        return

    masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) >= 8 else "***"
    print(f"[*] API Key detected: {masked_key} (length: {len(api_key)})")
    print(f"[*] API Secret detected: (length: {len(api_secret)})")
    print(f"[*] Testnet configured: {is_testnet}")
    print("-----------------------------------------------------------------")

    # 1. Test URLs
    futures_base = "https://testnet.binancefuture.com" if is_testnet else "https://fapi.binance.com"
    spot_base = "https://testnet.binance.vision" if is_testnet else "https://api.binance.com"

    # 2. Public Time Check & Latency
    print(f"[*] 1. Testing Futures Public Endpoint: {futures_base}/fapi/v1/time")
    t0 = time.time()
    status, data = http_get(f"{futures_base}/fapi/v1/time")
    latency_ms = int((time.time() - t0) * 1000)
    if status == 200:
        server_time = data.get("serverTime")
        local_time = int(time.time() * 1000)
        drift_ms = local_time - server_time
        print(f"    [PASS] HTTP 200 (Latency: {latency_ms}ms, Server Clock Drift: {drift_ms}ms)")
    else:
        print(f"    [FAIL] HTTP {status}: {data}")

    # 3. Authenticated Futures Test: /fapi/v1/positionSide/dual (Hedge Mode)
    print(f"[*] 2. Testing USDⓈ-M Futures Authentication & Hedge Mode...")
    server_timestamp = int(time.time() * 1000)
    params = {"timestamp": server_timestamp}
    signed_query = sign_query(api_secret, params)
    headers = {"X-MBX-APIKEY": api_key}
    
    url = f"{futures_base}/fapi/v1/positionSide/dual?{signed_query}"
    status, data = http_get(url, headers=headers)
    
    futures_authenticated = False
    if status == 200:
        futures_authenticated = True
        dual_side = data.get("dualSidePosition", False)
        mode_str = "HEDGE MODE (Dual-Side Position: Enabled)" if dual_side else "ONE-WAY MODE (Dual-Side Position: Disabled)"
        print(f"    [PASS] Authenticated successfully on USDⓈ-M Futures!")
        print(f"    [*] Position Mode: {mode_str}")
        if not dual_side:
            print("    [!] RECOMMENDATION: Blessing AI v0.2 requires Hedge Mode. You can enable it via Binance UI or API.")
    else:
        print(f"    [FAIL] USDⓈ-M Futures HTTP {status}: {data}")
        # If mainnet fails, test if it's a testnet key
        if not is_testnet and status == 401:
            print("    [*] Checking if key belongs to Testnet (testnet.binancefuture.com)...")
            t_url = f"https://testnet.binancefuture.com/fapi/v1/positionSide/dual?{signed_query}"
            t_status, t_data = http_get(t_url, headers=headers)
            if t_status == 200:
                print("    [!] NOTE: This key authenticated on BINANCE TESTNET! Set BINANCE_TESTNET=true in your environment.")

    # 4. Authenticated Futures Account Check: /fapi/v1/account
    if futures_authenticated:
        print(f"[*] 3. Fetching Futures Account Balances & Permissions...")
        url = f"{futures_base}/fapi/v1/account?{signed_query}"
        status, data = http_get(url, headers=headers)
        if status == 200:
            total_margin = data.get("totalMarginBalance", "0")
            available_balance = data.get("availableBalance", "0")
            can_trade = data.get("canTrade", False)
            can_deposit = data.get("canDeposit", False)
            can_withdraw = data.get("canWithdraw", False)
            print(f"    [PASS] Total Margin Balance: {total_margin} USDT")
            print(f"    [PASS] Available Balance: {available_balance} USDT")
            print(f"    [*] Can Trade: {can_trade} | Can Deposit: {can_deposit} | Can Withdraw: {can_withdraw}")
            if can_withdraw:
                print("    [!] WARNING: Withdrawal permission is ENABLED on this API Key. For trading bot security, disable Withdrawals in Binance API Management!")
            else:
                print("    [PASS] Security Check: Withdrawal permission is correctly disabled (Trading/Read only).")
        else:
            print(f"    [!] Error fetching futures account details: {data}")

    # 5. Authenticated Spot Test: /api/v3/account
    print(f"[*] 4. Testing Spot API Authentication: {spot_base}/api/v3/account...")
    s_params = {"timestamp": int(time.time() * 1000)}
    s_signed_query = sign_query(api_secret, s_params)
    s_url = f"{spot_base}/api/v3/account?{s_signed_query}"
    s_status, s_data = http_get(s_url, headers=headers)
    if s_status == 200:
        can_trade = s_data.get("canTrade", False)
        balances = [b for b in s_data.get("balances", []) if float(b.get("free", 0)) > 0 or float(b.get("locked", 0)) > 0]
        print(f"    [PASS] Spot API Authenticated! Can Trade: {can_trade}")
        print(f"    [*] Active Spot Assets: {len(balances)} tokens")
        for b in balances[:5]:
            print(f"        - {b['asset']}: Free={b['free']}, Locked={b['locked']}")
    else:
        print(f"    [INFO] Spot API returned HTTP {s_status}: {s_data.get('msg', s_data)}")

    # 6. API Restrictions Check: /sapi/v1/account/apiRestrictions
    if not is_testnet:
        print(f"[*] 5. Checking API Key Restrictions (/sapi/v1/account/apiRestrictions)...")
        r_params = {"timestamp": int(time.time() * 1000)}
        r_signed = sign_query(api_secret, r_params)
        r_url = f"https://api.binance.com/sapi/v1/account/apiRestrictions?{r_signed}"
        r_status, r_data = http_get(r_url, headers=headers)
        if r_status == 200:
            print("    [PASS] API Restrictions retrieved:")
            print(f"        - IP Restricted: {r_data.get('ipRestrict', False)}")
            print(f"        - Enable Spot & Margin: {r_data.get('enableSpotAndMarginTrading', False)}")
            print(f"        - Enable Futures: {r_data.get('enableFutures', False)}")
            print(f"        - Enable Withdrawals: {r_data.get('enableWithdrawals', False)}")
            print(f"        - Enable Reading: {r_data.get('enableReading', True)}")
        else:
            print(f"    [INFO] API Restrictions check: {r_data.get('msg', r_data)}")

    print("=================================================================")
    print("                     DIAGNOSTIC COMPLETE                         ")
    print("=================================================================")


if __name__ == "__main__":
    test_binance_connectivity()
