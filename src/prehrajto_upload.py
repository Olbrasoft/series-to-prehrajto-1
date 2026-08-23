#!/usr/bin/env python3
"""Přehraj.to — end-to-end upload bez prohlížeče.

Spuštění:
    export PREHRAJTO_EMAIL=...
    export PREHRAJTO_PASSWORD=...
    python3 prehrajto_upload.py /path/to/video.mp4 ["Display name"] ["Popis filmu"]
"""
import json
import os
import sys
from pathlib import Path
import requests

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0"
)
SEC_CH_UA = '"Not:A-Brand";v="99", "Microsoft Edge";v="145", "Chromium";v="145"'
ACCEPT_LANG = "cs,en;q=0.9,en-GB;q=0.8,en-US;q=0.7"


def _redact_cookie_value(name: str, value: str) -> str:
    if any(token in name.lower() for token in ("token", "session", "refresh", "access")):
        return "***"
    return value


def _safe_cookies(cookies) -> dict[str, str]:
    return {name: _redact_cookie_value(name, value) for name, value in dict(cookies).items()}


def _safe_set_cookie(header: str | None) -> str:
    if not header:
        return ""
    redacted_parts: list[str] = []
    for part in header.split(", "):
        if "=" not in part:
            redacted_parts.append(part)
            continue
        name, rest = part.split("=", 1)
        if any(token in name.lower() for token in ("token", "session", "refresh", "access")):
            rest = "***;" + rest.split(";", 1)[1] if ";" in rest else "***"
        redacted_parts.append(f"{name}={rest}")
    return ", ".join(redacted_parts)


def login(email: str, password: str) -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT

    # Prime the session — visit homepage to get initial Nette cookies (csrf, session)
    prime = s.get("https://prehraj.to/")
    print(f"[login] prime GET status={prime.status_code}, cookies={_safe_cookies(s.cookies)}")

    r = s.post(
        "https://prehraj.to/?frm=homepageLoginForm-loginForm",
        files={
            "email": (None, email),
            "password": (None, password),
            "_do": (None, "homepageLoginForm-loginForm-submit"),
            "login": (None, "Přihlásit se"),
        },
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "Referer": "https://prehraj.to/",
        },
        allow_redirects=False,
    )
    print(f"[login] status={r.status_code}, ctype={r.headers.get('content-type')}")
    print(f"[login] response text (first 500): {r.text[:500]!r}")
    print(f"[login] set-cookie headers: {_safe_set_cookie(r.headers.get('set-cookie'))!r}")
    print(f"[login] session cookies after login: {_safe_cookies(s.cookies)}")
    r.raise_for_status()

    check = s.get("https://prehraj.to/profil", allow_redirects=False)
    print(f"[login] /profil check status={check.status_code}")
    if check.status_code != 200:
        raise RuntimeError(f"Login failed — /profil vrací {check.status_code}")
    print(f"[login] OK, {len(s.cookies)} cookies uloženo")
    return s


def upload_video(
    session: requests.Session,
    path: Path,
    *,
    display_name: str | None = None,
    description: str = "",
    private: bool = False,
) -> int:
    size = path.stat().st_size
    final_name = display_name or path.name
    # Přehraj.to prepareVideo/CDN require a name ending in an extension.
    # If caller wants a clean display name (e.g. "Lví král (1994) CZ Dabing"),
    # we upload with ".mp4" appended and strip it via a rename call after.
    upload_name = final_name if "." in final_name else final_name + ".mp4"
    print(f"[upload] Soubor: {path.name} ({size} B), upload name: {upload_name}, final: {final_name}")

    # Priming GET /profil/nahrat-soubor — browser does this on page load; moderace
    # může matchovat (prepareVideo → CDN upload) proti sekvenci page visits.
    session.get(
        "https://prehraj.to/profil/nahrat-soubor",
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": ACCEPT_LANG,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
        },
    )

    print(f"[upload] Krok 1: prepareVideo")
    prep = session.post(
        "https://prehraj.to/profil/nahrat-soubor?do=prepareVideo",
        headers={
            "Accept": "*/*",
            "Accept-Language": ACCEPT_LANG,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://prehraj.to",
            "Referer": "https://prehraj.to/profil/nahrat-soubor",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
        },
        data={
            "description": description,
            "name": upload_name,
            "size": str(size),
            "type": "video/mp4",
            "erotic": "false",
            "folder": "",
            "private": "true" if private else "false",
        },
    )
    print(f"[upload] prepareVideo status={prep.status_code}")
    prep.raise_for_status()
    prep_data = prep.json()
    print(f"[upload] prepare response: {prep_data}")
    video_id = json.loads(prep_data["params"])["video_id"]
    print(f"[upload] video_id={video_id}")

    print(f"[upload] Krok 2: upload na api.premiumcdn.net")
    # CDN upload: field name MUST be "files" (plural), file part MUST be first
    # in the multipart body, then metadata. Browser order:
    # files, response, project, nonce, params, signature.
    # requests puts `files` before `data` — but if we pass everything as `files`
    # tuples we control order.
    with path.open("rb") as fh:
        multipart = [
            ("files", (upload_name, fh, "video/mp4")),
            ("response", (None, prep_data["response"])),
            ("project", (None, prep_data["project"])),
            ("nonce", (None, prep_data["nonce"])),
            ("params", (None, prep_data["params"])),
            ("signature", (None, prep_data["signature"])),
        ]
        r = requests.post(
            "https://api.premiumcdn.net/upload/",
            headers={
                "Accept": "*/*",
                "Accept-Language": ACCEPT_LANG,
                "Origin": "https://prehraj.to",
                "Referer": "https://prehraj.to/",
                "User-Agent": USER_AGENT,
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "cross-site",
                "sec-ch-ua": SEC_CH_UA,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Linux"',
            },
            files=multipart,
            timeout=3600,
        )
    print(f"[upload] CDN status={r.status_code}")
    print(f"[upload] CDN response: {r.text[:300]!r}")
    r.raise_for_status()

    # If caller wanted a display name different from upload_name (e.g. without
    # .mp4 extension), finalize via Přehraj.to's video-name rename endpoint.
    # Discovered by DevTools; Nette-style `do=…` action with param
    # `uploadedVideoListing-name`.
    if final_name != upload_name:
        print(f"[upload] Krok 3: rename na '{final_name}'")
        rn = session.post(
            f"https://prehraj.to/profil/nahrana-videa?uploadedVideoListing-videoId={video_id}&do=uploadedVideoListing-changeVideoName",
            headers={
                "Accept": "application/json",
                "Accept-Language": ACCEPT_LANG,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://prehraj.to",
                "Referer": "https://prehraj.to/profil/nahrana-videa",
                "X-Requested-With": "XMLHttpRequest",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "sec-ch-ua": SEC_CH_UA,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Linux"',
            },
            data={"uploadedVideoListing-name": final_name},
        )
        print(f"[upload] rename status={rn.status_code}")
        rn.raise_for_status()

    return video_id


def main() -> int:
    email = os.environ.get("PREHRAJTO_EMAIL")
    password = os.environ.get("PREHRAJTO_PASSWORD")
    if not email or not password:
        print("ERROR: Chybí PREHRAJTO_EMAIL nebo PREHRAJTO_PASSWORD v env")
        return 2
    if len(sys.argv) not in (2, 3, 4):
        print(f"Použití: {sys.argv[0]} /path/to/video.mp4 [\"Display name\"] [\"Popis filmu\"]")
        return 2

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"ERROR: soubor neexistuje: {path}")
        return 2
    display_name = sys.argv[2] if len(sys.argv) >= 3 else None
    description = sys.argv[3] if len(sys.argv) >= 4 else ""

    session = login(email, password)
    video_id = upload_video(
        session, path, display_name=display_name, description=description
    )
    print(f"\n=== HOTOVO ===")
    print(f"video_id: {video_id}")
    print(f"Zkontroluj v profilu: https://prehraj.to/profil/nahrana-videa")
    return 0


if __name__ == "__main__":
    sys.exit(main())
