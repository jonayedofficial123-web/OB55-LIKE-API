#MEHEDI
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent

API_URL = os.getenv(
    "FF_JWT_API_BASE",
    "https://ff-jwt-gen-api.lovable.app/api/public/token",
)
TIMEOUT = float(os.getenv("FF_JWT_TIMEOUT", "30"))
MAX_RETRIES = max(1, int(os.getenv("FF_JWT_MAX_RETRIES", "4")))
DELAY = max(0, float(os.getenv("FF_JWT_DELAY", "0.3")))

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "OB54-JWT-Fetcher/2.0",
}

FILE_PAIRS = [
    ("Uid_ind.json", "token_ind.json"),
    ("Uid_bd.json", "token_bd.json"),
]


def extract_token(data):
    if isinstance(data, str):
        value = data.strip()
        parts = value.split(".")
        if len(parts) == 3 and all(parts):
            return value
        return None

    if isinstance(data, dict):
        for key in (
            "jwt_token", "jwt", "token",
            "access_token", "result",
        ):
            value = data.get(key)
            if isinstance(value, str):
                parts = value.strip().split(".")
                if len(parts) == 3 and all(parts):
                    return value.strip()

        for key in ("data", "response", "payload"):
            if key in data:
                token = extract_token(data[key])
                if token:
                    return token

    if isinstance(data, list):
        for item in data:
            token = extract_token(item)
            if token:
                return token

    return None


def get_jwt(uid, password, session):
    last_error = "Token request failed"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(
                API_URL,
                params={"uid": uid, "password": password},
                headers=HEADERS,
                timeout=TIMEOUT,
            )

            if response.status_code == 200:
                try:
                    body = response.json()
                except ValueError:
                    body = response.text

                token = extract_token(body)
                if token:
                    return token

                last_error = "API response did not contain a valid JWT"

            elif response.status_code in (
                408, 425, 429, 500, 502, 503, 504
            ):
                last_error = f"HTTP {response.status_code}"

            else:
                raise RuntimeError(
                    f"HTTP {response.status_code}: "
                    f"{response.text[:150]}"
                )

        except requests.RequestException as exc:
            last_error = f"Network error: {exc}"

        if attempt < MAX_RETRIES:
            time.sleep(min(attempt * 2, 8))

    raise RuntimeError(last_error)


def process_file(input_name, output_name, session):
    input_path = BASE_DIR / input_name
    output_path = BASE_DIR / output_name

    if not input_path.is_file():
        print(f"[SKIP] {input_name} not found")
        return False

    try:
        accounts = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ERROR] {input_name}: {exc}")
        return False

    if not isinstance(accounts, list):
        print(f"[ERROR] {input_name} must contain a JSON list")
        return False

    tokens = []
    failures = []

    for index, account in enumerate(accounts, 1):
        if not isinstance(account, dict):
            failures.append((index, "Invalid account entry"))
            continue

        uid = str(account.get("uid", "")).strip()
        password = str(account.get("password", "")).strip()

        if not uid or not password:
            failures.append((index, "Missing uid/password"))
            continue

        try:
            jwt = get_jwt(uid, password, session)
            tokens.append({"token": jwt})
            print(f"[OK] {input_name}: {index}/{len(accounts)}")
        except Exception as exc:
            failures.append((index, str(exc)))
            print(f"[FAILED] {input_name}: account #{index}: {exc}")

        if DELAY:
            time.sleep(DELAY)

    # Never replace an existing output unless every account succeeds.
    if failures:
        print(
            f"[PRESERVED] {output_name} unchanged; "
            f"{len(failures)} account(s) failed."
        )
        return False

    temp_path = output_path.with_name(output_path.name + ".tmp")

    try:
        temp_path.write_text(
            json.dumps(tokens, ensure_ascii=False, indent=4) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, output_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    print(f"[SUCCESS] {len(tokens)} tokens saved to {output_name}")
    return True


def main():
    session = requests.Session()
    session.trust_env = True

    found = False
    all_ok = True

    try:
        for input_name, output_name in FILE_PAIRS:
            if (BASE_DIR / input_name).is_file():
                found = True
                if not process_file(input_name, output_name, session):
                    all_ok = False

        if not found:
            print("ERROR: Neither Uid_ind.json nor Uid_bd.json was found.")
            return 2

        return 0 if all_ok else 1

    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
