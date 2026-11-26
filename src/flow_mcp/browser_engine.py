"""Browser-driven Google Flow Engine (No API Key Required).

Uses your logged-in Google account and Google AI Ultra / Pro credits directly on:
- Google Flow (`https://labs.google/fx/tools/flow`)
- Google Whisk (`https://labs.google/fx/tools/whisk`)

Maintains a persistent Chrome profile at `~/.google-flow-mcp/chrome-profile` so you only
sign in once with your Google email, and Claude / ChatGPT can trigger video & image
generations using your subscription credits.
"""

from __future__ import annotations

import os
import re
import sys
import time
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

# hl=en forces English even when the Google account uses another language,
# so the tool can find buttons like "New project" and "Approve".
FLOW_URL = "https://flow.google.com/?hl=en"  # labs.google/fx/tools/flow redirects here


def _ensure_english(page: Any) -> None:
    """Reload the current Flow page with hl=en if it isn't already (e.g. after opening a project)."""
    # "New project" navigates to /project/... a moment later and drops hl=en; wait for it first.
    try:
        page.wait_for_url(re.compile(r"/project/"), timeout=15000)
    except Exception:
        pass
    url = page.url
    if "flow.google.com" in url and "hl=en" not in url:
        _goto_flow(page, url + ("&" if "?" in url else "?") + "hl=en")
        time.sleep(4)


def _goto_flow(page: Any, url: str = FLOW_URL) -> None:
    """Open a Flow page, retrying once if Chrome aborts the first load (e.g. during a redirect)."""
    for attempt in range(3):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return
        except Exception as e:
            if "ERR_ABORTED" not in str(e) or attempt == 2:
                raise
            time.sleep(3)
WHISK_URL = "https://labs.google/fx/tools/whisk"


def get_profile_dir() -> str:
    """Returns the persistent Chrome profile directory where your Google login lives."""
    default_dir = os.path.expanduser("~/.google-flow-mcp/chrome-profile")
    profile_dir = os.environ.get("FLOW_CHROME_PROFILE", default_dir)
    os.makedirs(profile_dir, exist_ok=True)
    return profile_dir


def get_media_dir() -> str:
    """Returns the directory where generated videos and images are saved."""
    # Default to a folder people can find, also when installed with uvx/pipx.
    media_dir = os.path.expanduser(os.environ.get("FLOW_MEDIA_DIR") or "~/Downloads/google-flow")
    os.makedirs(media_dir, exist_ok=True)
    return media_dir


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:45] or "flow_output"


def _want_headless(headless: Optional[bool]) -> bool:
    """Generation runs hidden (no Chrome window) unless FLOW_SHOW_BROWSER=1 or headless=False is passed."""
    if headless is not None:
        return headless
    return os.getenv("FLOW_SHOW_BROWSER", "").strip().lower() not in ("1", "true", "yes")


def _launch_persistent_context(playwright: Any, headless: bool = False):
    """Launches Google Chrome with the persistent user profile."""
    profile_dir = get_profile_dir()
    # Remove stale SingletonLock if a previous Chrome instance crashed
    lock_file = os.path.join(profile_dir, "SingletonLock")
    if os.path.lexists(lock_file):
        try:
            os.remove(lock_file)
        except OSError:
            pass

    # Google signs out real headless Chrome, so "hidden" runs use a normal Chrome window
    # placed far off-screen instead. It stays signed in but never shows on your screen.
    hidden_args = (
        [
            "--window-position=-32000,-32000",
            "--window-size=1440,900",
            # Keep the page running at full speed while minimized.
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ]
        if headless
        else []
    )
    front_app = _front_app_bundle_id() if headless else None
    if headless:
        _hide_chrome_as_it_starts(profile_dir)
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=profile_dir,
        channel="chrome",
        headless=False,
        # Keep Chrome's sandbox on (Playwright otherwise passes --no-sandbox).
        chromium_sandbox=True,
        accept_downloads=True,
        # Force English so button and label matching works for accounts set to other languages.
        locale="en-US",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        viewport={"width": 1440, "height": 900},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--lang=en-US",
            *hidden_args,
        ],
    )
    if headless:
        # macOS pulls off-screen windows back onto the screen, so also minimize the window
        # (and any tab Flow or the tool opens later) to keep it out of sight.
        context.on("page", lambda pg: _minimize_window(context, pg))
        for pg in context.pages:
            _minimize_window(context, pg)
        _give_focus_back(front_app)
    return context


def _hide_chrome_as_it_starts(profile_dir: str, seconds: float = 900.0) -> None:
    """macOS: hide the tool's Chrome (like Cmd+H) the moment it starts, and keep it hidden while
    it loads, so no window shows up. Only touches the Chrome that uses the tool's own profile."""
    if sys.platform != "darwin":
        return
    import subprocess
    import threading

    def hide_pid(pid: str) -> None:
        subprocess.run(
            ["osascript", "-l", "JavaScript", "-e",
             f'ObjC.import("AppKit"); var a = $.NSRunningApplication.runningApplicationWithProcessIdentifier({int(pid)}); if (a) {{ a.hide; }}'],
            capture_output=True,
            timeout=5,
        )

    def watch() -> None:
        deadline = time.time() + seconds
        seen = False
        while time.time() < deadline:
            try:
                out = subprocess.run(
                    ["pgrep", "-f", "--", f"user-data-dir={profile_dir}"], capture_output=True, text=True, timeout=3
                ).stdout.split()
                if seen and not out:
                    return  # Chrome closed: generation finished
                seen = seen or bool(out)
                # The main Chrome process is the one without "--type=" (helpers have it).
                for pid in out:
                    cmd = subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True).stdout
                    if "--type=" not in cmd:
                        hide_pid(pid)
            except Exception:
                pass
            time.sleep(0.3 if time.time() > deadline - seconds + 20 else 0.2)

    threading.Thread(target=watch, daemon=True).start()


def _front_app_bundle_id() -> Optional[str]:
    """Bundle id of the app you're using (macOS), so focus can go back to it after Chrome starts."""
    if sys.platform != "darwin":
        return None
    try:
        import subprocess

        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=3).stdout.strip()
        out = subprocess.run(
            ["lsappinfo", "info", "-only", "bundleid", asn], capture_output=True, text=True, timeout=3
        ).stdout
        m = re.search(r'"([^"]+)"\s*$', out.strip())
        return m.group(1) if m else None
    except Exception:
        return None


def _give_focus_back(bundle_id: Optional[str]) -> None:
    """Chrome grabs focus when it starts; hand it back to the app you were using."""
    if not bundle_id or bundle_id == "com.google.Chrome":
        return
    try:
        import subprocess

        subprocess.run(
            ["osascript", "-e", f'tell application id "{bundle_id}" to activate'],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def _minimize_window(context: Any, page: Any) -> None:
    try:
        cdp = context.new_cdp_session(page)
        window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "minimized"}})
        cdp.detach()
    except Exception:
        pass


_LANDING_CTA_RE = re.compile(r"create with (google )?flow|get started|start creating", re.I)


def _click_landing_cta(page: Any) -> bool:
    """Click the public landing page's call-to-action (a button or a link). Returns True if clicked."""
    for role in ("button", "link"):
        cta = page.get_by_role(role, name=_LANDING_CTA_RE)
        if cta.count() > 0 and cta.first.is_visible():
            cta.first.click()
            time.sleep(4)
            return True
    return False


def _is_in_flow_workspace(page: Any) -> bool:
    """True only when a signed-in Flow workspace is showing, not the public landing or sign-in page."""
    url = page.url
    if "accounts.google.com" in url or "/about" in url:
        return False
    # The public landing page's CTA means we are not inside the app yet.
    for role in ("button", "link"):
        cta = page.get_by_role(role, name=re.compile(r"create with (google )?flow", re.I))
        if cta.count() > 0 and cta.first.is_visible():
            return False
    has_textarea = page.locator("textarea:visible").count() > 0
    new_project = page.get_by_text(re.compile(r"new project", re.I))
    has_new_project = new_project.count() > 0 and new_project.first.is_visible()
    if has_textarea or has_new_project:
        return True
    # Works in any language: Flow's account button label contains the signed-in email.
    try:
        return "flow.google.com" in url and bool(
            page.evaluate(
                """() => [...document.querySelectorAll('[aria-label]')]
                      .some(e => /[\\w.+-]+@[\\w-]+\\.[\\w.-]+/.test(e.getAttribute('aria-label')))"""
            )
        )
    except Exception:
        return False


def _read_credit_balance(page: Any) -> Optional[int]:
    """Read "N Google Flow credits" from the Account details (PRO) menu. Returns None if not found."""
    try:
        btn = page.get_by_role("button", name="Account details")
        if btn.count() == 0:
            return None
        btn.first.click()
        time.sleep(2)
        m = re.search(r"([\d,]+)\s+Google Flow credits", page.inner_text("body"), re.I)
        return int(m.group(1).replace(",", "")) if m else None
    except Exception:
        return None
    finally:
        _close_overlays(page)


def _close_overlays(page: Any) -> None:
    """Close open menus. Escape alone can leave Flow's invisible backdrop up, which blocks later clicks."""
    try:
        page.keyboard.press("Escape")
        time.sleep(0.5)
        for i in range(6):
            backdrop = page.locator(".cdk-overlay-backdrop-showing")
            if backdrop.count() == 0:
                break
            # Alternate: some menus only close on a backdrop click, others only on Escape.
            if i % 2 == 0:
                backdrop.last.click(force=True)
            else:
                page.keyboard.press("Escape")
            time.sleep(0.7)
    except Exception:
        pass


def _credits_after(page: Any, before: Optional[int]) -> Dict[str, Any]:
    """Go back to the Flow home page, read the balance again, and report what was used."""
    project_url = page.url
    after = None
    try:
        _goto_flow(page)
        time.sleep(5)
        after = _read_credit_balance(page)
    except Exception:
        pass
    used = before - after if before is not None and after is not None else None
    account = _signed_in_email(page)
    return {
        "credits_before": before,
        "credits_after": after,
        "credits_used": used,
        "flow_account": account,
        "flow_project_url": _account_link(project_url, account),
    }


def _signed_in_email(page: Any) -> Optional[str]:
    """Email of the Google account Flow is using, read from the account button's label."""
    try:
        labels = page.evaluate(
            """() => [...document.querySelectorAll('[aria-label]')]
                  .map(e => e.getAttribute('aria-label'))
                  .filter(l => /Google Account/i.test(l))"""
        )
        for label in labels:
            m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", label)
            if m:
                return m.group(0)
    except Exception:
        pass
    return None


def _account_link(url: str, email: Optional[str]) -> str:
    """Add authuser=<email> so the link opens in the account that made it, even if the
    browser's default Google account is a different one."""
    if not email or "flow.google.com" not in url:
        return url
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["authuser"] = email
    return urlunsplit(parts._replace(query=urlencode(query)))


def login_and_verify_flow_session(wait_seconds: int = 120) -> Dict[str, Any]:
    """Opens Google Flow in a visible Chrome window so the user can log in with their Google email,
    or verifies that the session is already logged in and ready."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = _launch_persistent_context(p, headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            _goto_flow(page)
            time.sleep(4)

            # On the public landing page, click "Create with Google Flow" once so the
            # user is taken to Google sign-in (or straight into the app if already signed in).
            _click_landing_cta(page)

            start_time = time.time()
            logged_in = False

            while time.time() - start_time < wait_seconds:
                try:
                    if _is_in_flow_workspace(page):
                        logged_in = True
                        break
                    # After sign-in Google often returns to Flow's welcome page: click through it.
                    if "flow.google.com" in page.url and int(time.time() - start_time) % 10 < 2:
                        _click_landing_cta(page)
                except Exception:
                    # Page may be mid-navigation during sign-in; keep waiting.
                    pass
                time.sleep(2)

            screenshot_path = os.path.join(get_media_dir(), "flow_session_status.png")
            page.screenshot(path=screenshot_path)

            return {
                "status": "authenticated" if logged_in else "waiting_for_login",
                "current_url": page.url,
                "profile_dir": get_profile_dir(),
                "screenshot_path": screenshot_path,
                "message": (
                    "Google Flow session is logged in and ready to use your subscription credits!"
                    if logged_in
                    else "Chrome window opened to Google Flow. Please finish signing in with your Google email and run again."
                ),
            }
        finally:
            context.close()


def generate_video_via_flow_browser(
    prompt: str,
    mode: str = "text_to_video",
    first_frame: Optional[str] = None,
    last_frame: Optional[str] = None,
    reference_images: Optional[List[str]] = None,
    aspect_ratio: str = "16:9",
    duration: Optional[int] = None,
    output_path: Optional[str] = None,
    timeout_seconds: int = 600,
    headless: Optional[bool] = None,
) -> Dict[str, Any]:
    """Automates the live Google Flow web UI (`labs.google/fx/tools/flow`) using your logged-in
    Google account and AI credits (no API key needed)."""
    from playwright.sync_api import sync_playwright

    if not output_path:
        filename = f"flow_browser_{_slugify(prompt)}_{uuid.uuid4().hex[:6]}.mp4"
        output_path = os.path.join(get_media_dir(), filename)
    else:
        output_path = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

    captured_video_urls: List[str] = []

    def _on_response(response: Any) -> None:
        try:
            ct = response.headers.get("content-type", "").lower()
            url = response.url
            if "video/mp4" in ct or url.endswith(".mp4") or ("storage.googleapis.com" in url and "video" in ct):
                if url not in captured_video_urls:
                    captured_video_urls.append(url)
        except Exception:
            pass

    with sync_playwright() as p:
        context = _launch_persistent_context(p, headless=_want_headless(headless))
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.on("response", _on_response)
            _goto_flow(page)
            time.sleep(5)

            # 1. If on the public landing page, click "Create with Google Flow" (button or link)
            credits_before = _read_credit_balance(page) if "/about" not in page.url else None
            if not _click_landing_cta(page):
                credits_before = credits_before if credits_before is not None else _read_credit_balance(page)
                btn = page.get_by_role("button", name=re.compile(r"new project", re.I))
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    time.sleep(4)

            # Also check links/divs with "New project"
            new_proj_text = page.get_by_text(re.compile(r"^\+?\s*New project$", re.I))
            if new_proj_text.count() > 0 and new_proj_text.first.is_visible():
                new_proj_text.first.click()
                time.sleep(4)
            _ensure_english(page)

            # Verify not stuck on Google login
            if "accounts.google.com" in page.url:
                raise RuntimeError(
                    "You are not signed in to Google Flow yet. Call `flow_browser_login` first and sign in with your Google email in the Chrome window."
                )
            if "/about" in page.url:
                raise RuntimeError(
                    f"Still on the Google Flow landing page ({page.url}); could not open the Flow app. "
                    "Call `flow_browser_login` and finish signing in."
                )

            # 2. Select generation mode if mode dropdown exists (Text to Video / Frames to Video / Ingredients to Video)
            target_mode_label = {
                "text_to_video": "Text to Video",
                "frames_to_video": "Frames to Video",
                "reference_to_video": "Ingredients to Video",
            }.get(mode, "Text to Video")

            mode_selector = page.get_by_text(
                re.compile(r"Text to Video|Frames to Video|Ingredients to Video", re.I)
            )
            if mode_selector.count() > 0 and mode_selector.first.is_visible():
                current_mode_text = mode_selector.first.inner_text()
                if target_mode_label.lower() not in current_mode_text.lower():
                    mode_selector.first.click()
                    time.sleep(1)
                    option = page.get_by_text(re.compile(target_mode_label, re.I))
                    if option.count() > 0:
                        option.last.click()
                        time.sleep(1.5)

            # 3. Upload frames or reference images if provided
            files_to_upload: List[str] = []
            if first_frame and os.path.exists(first_frame):
                files_to_upload.append(os.path.abspath(first_frame))
            if last_frame and os.path.exists(last_frame):
                files_to_upload.append(os.path.abspath(last_frame))
            if reference_images:
                for img in reference_images:
                    if os.path.exists(img):
                        files_to_upload.append(os.path.abspath(img))

            if files_to_upload:
                file_inputs = page.locator('input[type="file"]')
                if file_inputs.count() > 0:
                    file_inputs.first.set_input_files(files_to_upload)
                    time.sleep(4)

            # 4. Fill the prompt box. Flow's current editor uses a contenteditable
            # div (role=textbox, "What do you want to create?"); older UIs used a <textarea>.
            # Note: a hidden reCAPTCHA <textarea> exists on the page, so only match visible ones.
            prompt_box = page.get_by_role("textbox", name=re.compile(r"what do you want to create|describe your goal", re.I))
            if prompt_box.count() == 0:
                prompt_box = page.locator(
                    'textarea:visible, [contenteditable="true"][role="textbox"]:visible'
                )
            prompt_box = prompt_box.first
            try:
                prompt_box.wait_for(state="visible", timeout=30000)
            except Exception:
                raise RuntimeError(
                    f"Could not find the Flow prompt box on {page.url}. The Flow UI may have changed."
                )

            existing_videos = page.locator("video").count()
            initial_captured_count = len(captured_video_urls)

            # The editor is agent-style, so say explicitly that we want a video.
            # Flow ignores length unless the prompt states it, so say it explicitly.
            length = f"{duration}-second " if duration else ""
            full_prompt = f"Create a {length}video ({aspect_ratio}): {prompt}"
            flow_settings = _apply_flow_settings(page, aspect_ratio, count=1, kind="video")

            prompt_box.click()
            prompt_box.fill(full_prompt)
            time.sleep(1)

            # 5. Click "Start generation". Avoid loose patterns like "generate"/"create":
            # they match suggestion chips ("Generate concept art") and "Create collection".
            submitted = False
            for submit_pattern in [r"^start generation$", r"^generate$", r"^submit$", r"^send$"]:
                gen_btn = page.get_by_role("button", name=re.compile(submit_pattern, re.I))
                if gen_btn.count() > 0 and gen_btn.last.is_visible() and gen_btn.last.is_enabled():
                    gen_btn.last.click()
                    submitted = True
                    break

            if not submitted:
                prompt_box.press("Enter")

            # 6. Approve the credit charge (Flow's agent asks before each generation),
            #    then wait for the new video to render.
            initial_video_srcs = set(_video_srcs(page))
            initial_thumbs = set(_thumbnail_srcs(page))
            start_wait = time.time()
            downloaded_file: Optional[str] = None
            final_video_src: Optional[str] = None
            approved = False
            credits_cost: Optional[int] = None

            try:
                while time.time() - start_wait < timeout_seconds:
                    if not approved:
                        # Click "Approve" for this one generation only, never "Always approve in project".
                        # Flow renders the choices as role="radio" with aria-label "Approve"; exact match
                        # keeps "Always approve in project" out. Fall back to a button for older UIs.
                        approve_btn = page.get_by_role("radio", name="Approve", exact=True)
                        if approve_btn.count() == 0:
                            approve_btn = page.get_by_role("button", name="Approve", exact=True)
                        if approve_btn.count() > 0 and approve_btn.last.is_visible():
                            m = re.search(r"costing\s+(\d+)\s+credits", page.inner_text("body"), re.I)
                            credits_cost = int(m.group(1)) if m else None
                            approve_btn.last.click()
                            approved = True
                            time.sleep(3)
                            continue

                    # A new network MP4 response was captured
                    if len(captured_video_urls) > initial_captured_count:
                        final_video_src = captured_video_urls[-1]
                        break

                    # Flow shows a finished clip as a thumbnail and only creates the <video>
                    # element on hover, so hover any new "Generated video thumbnail".
                    if approved:
                        _hover_new_video_thumbnails(page, initial_thumbs)

                    # Or a new <video> element appeared
                    new_srcs = [s for s in _video_srcs(page) if s not in initial_video_srcs]
                    if new_srcs:
                        final_video_src = new_srcs[-1]
                        break

                    time.sleep(3)

                if approved and credits_cost is None:
                    costs = re.findall(r"costing\s+(\d+)\s+credits", page.inner_text("body"), re.I)
                    credits_cost = int(costs[-1]) if costs else None

                # Save a UI screenshot of the result
                ui_screenshot = os.path.splitext(output_path)[0] + "_flow_ui.png"
                page.screenshot(path=ui_screenshot)

                if final_video_src:
                    data = _download_video(context, page, final_video_src)
                    if data:
                        with open(output_path, "wb") as f:
                            f.write(data)
                        downloaded_file = output_path
            except Exception as e:
                if "closed" in str(e).lower():
                    raise RuntimeError(
                        "The Chrome window was closed before the video finished. "
                        f"Check the newest project in Google Flow for the clip. (approved={approved})"
                    )
                raise

            if downloaded_file:
                status = "completed"
                message = f"Video generated with your Google Flow credits and saved to {downloaded_file}"
            elif approved:
                status = "timed_out_waiting_for_video"
                message = (
                    f"Generation was approved but the video was not ready within {timeout_seconds}s. "
                    f"It should appear in your Flow project: {page.url}"
                )
            else:
                status = "not_started"
                message = (
                    "Prompt was sent but Flow never asked to approve a generation, so no video was started. "
                    f"See the screenshot: {ui_screenshot}"
                )

            credit_info = _credits_after(page, credits_before)
            if credit_info["credits_used"] is not None:
                credits_cost = credit_info["credits_used"]
            return {
                **credit_info,
                "status": status,
                "engine": "google_flow_browser_credits",
                "mode": mode,
                "flow_settings": flow_settings,
                "prompt": full_prompt,
                "credits_cost": credits_cost,
                "output_path": downloaded_file,
                "ui_screenshot": ui_screenshot,
                "video_src": final_video_src if final_video_src and final_video_src.startswith("http") else None,
                "message": message,
            }
        finally:
            context.close()


def _video_srcs(page: Any) -> List[str]:
    """Current sources of all <video> elements on the page (handles <source> children and blob: URLs)."""
    return page.evaluate(
        """() => [...document.querySelectorAll('video')]
              .map(v => v.currentSrc || v.src || (v.querySelector('source') || {}).src || '')
              .filter(Boolean)"""
    )


_THUMB_SELECTOR = 'img[alt="Generated video thumbnail"]'


def _thumbnail_srcs(page: Any) -> List[str]:
    """Sources of the generated-video thumbnails currently shown in the project grid."""
    return page.evaluate(
        "(sel) => [...document.querySelectorAll(sel)].map(i => i.src).filter(Boolean)", _THUMB_SELECTOR
    )


def _hover_new_video_thumbnails(page: Any, seen: set) -> None:
    """Hover thumbnails that weren't there before generation so Flow loads their <video>."""
    thumbs = page.locator(_THUMB_SELECTOR)
    for i in range(thumbs.count()):
        t = thumbs.nth(i)
        src = t.get_attribute("src") or ""
        if src and src not in seen and t.is_visible():
            t.hover()
            time.sleep(2)


def _download_video(context: Any, page: Any, src: str) -> Optional[bytes]:
    """Download a video by URL through the signed-in browser. Supports http(s) and blob: URLs."""
    if src.startswith("http"):
        for _ in range(3):
            try:
                resp = context.request.get(src, timeout=180000)
                if resp.ok and resp.body():
                    return resp.body()
            except Exception:
                time.sleep(3)
        return None
    if src.startswith("blob:"):
        import base64

        b64 = page.evaluate(
            """async (u) => {
                const buf = await (await fetch(u)).arrayBuffer();
                let s = ''; const bytes = new Uint8Array(buf);
                for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
                return btoa(s);
            }""",
            src,
        )
        return base64.b64decode(b64) if b64 else None
    return None


# ---------------------------------------------------------------------------
# Image generation with Flow credits (Nano Banana via Flow's assistant)
# ---------------------------------------------------------------------------


def _flow_images(page: Any) -> List[Dict[str, Any]]:
    """Generated images in the project grid. Flow serves them from /asb/ URLs; video thumbnails are excluded."""
    return page.evaluate(
        """() => [...document.querySelectorAll('img')]
              .filter(i => /flow\\.google\\.com\\/asb\\//.test(i.src) && !/video/i.test(i.alt || ''))
              .map(i => ({src: i.src, alt: i.alt || ''}))"""
    )


_FLOW_RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16")
_NEAREST_RATIO = {"21:9": "16:9", "3:2": "4:3", "5:4": "4:3", "2:3": "3:4", "4:5": "3:4", "9:21": "9:16"}


def _apply_flow_settings(
    page: Any, aspect_ratio: Optional[str], count: int = 1, kind: Optional[str] = None
) -> Dict[str, Any]:
    """Set aspect ratio and number of results in Flow's settings menu (the model button next to the
    prompt). Returns what was applied. Best effort: older Flow layouts without this menu are skipped."""
    applied: Dict[str, Any] = {}
    ratio = (aspect_ratio or "").strip()
    ratio = ratio if ratio in _FLOW_RATIOS else _NEAREST_RATIO.get(ratio, "")
    try:
        trigger = page.get_by_role("button", name="Settings trigger")
        if trigger.count() == 0:
            return applied
        trigger.first.click()
        time.sleep(1.5)
        if kind in ("image", "video"):
            label = r"Video$" if kind == "video" else r"(Image|Images)$"
            opt = page.get_by_role("radio", name=re.compile(label))
            if opt.count() > 0:
                opt.first.click()
                applied["kind"] = kind
                time.sleep(1)
        if ratio:
            opt = page.get_by_role("radio", name=re.compile(rf"(^|\s){re.escape(ratio)}$"))
            if opt.count() > 0:
                opt.first.click()
                applied["aspect_ratio"] = ratio
                time.sleep(0.5)
        if count:
            opt = page.get_by_role("radio", name=re.compile(rf"^x{int(count)}$"))
            if opt.count() > 0:
                opt.first.click()
                applied["count"] = int(count)
                time.sleep(0.5)
    except Exception:
        pass
    finally:
        _close_overlays(page)
    return applied


def _project_images_fresh(context: Any, project_url: str) -> List[str]:
    """Load the project in a separate tab and list its images. Works even when the live page
    missed the update, was reloaded, or closed (seen with Flow's direct Nano Banana mode)."""
    checker = context.new_page()
    try:
        checker.goto(project_url, wait_until="domcontentloaded", timeout=60000)
        deadline = time.time() + 15
        imgs: List[str] = []
        while time.time() < deadline:
            imgs = [i["src"] for i in _flow_images(checker)]
            if imgs:
                break
            time.sleep(1.5)
        return imgs
    except Exception:
        return []
    finally:
        try:
            checker.close()
        except Exception:
            pass


def _live_page(context: Any, page: Any, project_url: Optional[str]) -> Any:
    """Return an open page, reopening the project if Flow closed the tab we were watching."""
    if not page.is_closed():
        return page
    for other in reversed(context.pages):
        if not other.is_closed():
            return other
    fresh = context.new_page()
    if project_url:
        fresh.goto(project_url, wait_until="domcontentloaded", timeout=60000)
    return fresh


def _full_size_image_url(src: str) -> str:
    """Flow shows a small preview (e.g. `=s512-rw`); `=s0` asks for the original size."""
    base = re.sub(r"=[^/=]*$", "", src)
    return base + "=s0"


def _download_flow_image(context: Any, src: str) -> Optional[tuple]:
    """Download a Flow image at full size. Returns (bytes, content_type) or None."""
    # Full size first, then Flow's preview size; Google can be slow to serve the original.
    for url, timeout_ms in ((_full_size_image_url(src), 90000), (src, 60000), (src, 60000)):
        try:
            resp = context.request.get(url, timeout=timeout_ms)
            if resp.ok and resp.body():
                return resp.body(), resp.headers.get("content-type", "")
        except Exception:
            continue
    return None


def _ext_for(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    return ".jpg"


def generate_image_via_flow_browser(
    prompt: str,
    aspect_ratio: str = "16:9",
    reference_images: Optional[List[str]] = None,
    output_path: Optional[str] = None,
    timeout_seconds: int = 240,
    headless: Optional[bool] = None,
) -> Dict[str, Any]:
    """Generate an image in Google Flow with your signed-in account and credits (no API key).

    Flow's assistant picks the image model (Nano Banana); the prompt asks for an image explicitly.
    """
    from playwright.sync_api import sync_playwright

    if not output_path:
        output_path = os.path.join(get_media_dir(), f"flow_image_{_slugify(prompt)}_{uuid.uuid4().hex[:6]}.jpg")
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with sync_playwright() as p:
        context = _launch_persistent_context(p, headless=_want_headless(headless))
        try:
            page = context.pages[0] if context.pages else context.new_page()
            _goto_flow(page)
            time.sleep(5)
            _click_landing_cta(page)
            if "accounts.google.com" in page.url or "/about" in page.url:
                raise RuntimeError("Not signed in to Google Flow. Call `flow_browser_login` first.")
            credits_before = _read_credit_balance(page)

            new_project = page.get_by_text(re.compile(r"new project", re.I))
            if new_project.count() > 0 and new_project.first.is_visible():
                new_project.first.click()
                time.sleep(5)
            _ensure_english(page)

            if reference_images:
                files = [os.path.abspath(f) for f in reference_images if os.path.exists(f)]
                file_inputs = page.locator('input[type="file"]')
                if files and file_inputs.count() > 0:
                    file_inputs.first.set_input_files(files)
                    time.sleep(4)

            prompt_box = page.get_by_role("textbox", name=re.compile(r"what do you want to create|describe your goal", re.I)).first
            try:
                prompt_box.wait_for(state="visible", timeout=30000)
            except Exception:
                raise RuntimeError(f"Could not find the Flow prompt box on {page.url}. The Flow UI may have changed.")

            flow_settings = _apply_flow_settings(page, aspect_ratio, count=1, kind="image")
            before = {i["src"] for i in _flow_images(page)}
            full_prompt = f"Create an image ({aspect_ratio}): {prompt}"
            prompt_box.click()
            prompt_box.fill(full_prompt)
            time.sleep(1)
            start_btn = page.get_by_role("button", name=re.compile(r"^start generation$", re.I))
            if start_btn.count() > 0 and start_btn.last.is_enabled():
                start_btn.last.click()
            else:
                prompt_box.press("Enter")

            approved = False
            credits_cost: Optional[int] = None
            found: Optional[str] = None
            project_url: Optional[str] = page.url if "/project/" in page.url else None
            saved: Optional[str] = None
            ui_screenshot = os.path.splitext(output_path)[0] + "_flow_ui.png"
            start = time.time()
            last_fresh_check = start
            while time.time() - start < timeout_seconds:
                try:
                    page = _live_page(context, page, project_url)
                    if not project_url and "/project/" in page.url:
                        project_url = page.url.split("?")[0]
                    if not approved:
                        approve = page.get_by_role("radio", name="Approve", exact=True)
                        if approve.count() > 0 and approve.last.is_visible():
                            costs = re.findall(r"costing\s+(\d+)\s+credits", page.inner_text("body"), re.I)
                            credits_cost = int(costs[-1]) if costs else None
                            approve.last.click()  # this one generation only, never "Always approve"
                            approved = True
                            time.sleep(3)
                            continue
                    new = [i["src"] for i in _flow_images(page) if i["src"] not in before]
                    if new:
                        found = new[-1]
                        break
                except Exception:
                    pass  # page mid-reload or closed; the fresh check below still works
                # Every 25s, also load the project in its own tab: the live page sometimes never shows
                # the finished image (Flow's direct Nano Banana mode), but a fresh load does.
                if project_url and time.time() - last_fresh_check > 25:
                    last_fresh_check = time.time()
                    new = [src for src in _project_images_fresh(context, project_url) if src not in before]
                    if new:
                        found = new[0]
                        break
                time.sleep(3)

            try:
                page = _live_page(context, page, project_url)
                page.screenshot(path=ui_screenshot)
            except Exception:
                pass
            if found:
                got = _download_flow_image(context, found)
                if got:
                    data, ctype = got
                    saved = os.path.splitext(output_path)[0] + _ext_for(ctype)
                    with open(saved, "wb") as f:
                        f.write(data)

            if saved:
                status, message = "completed", f"Image generated with your Google Flow credits and saved to {saved}"
            elif found:
                status, message = "download_failed", f"Image was generated but could not be downloaded. See {project_url or page.url}"
            else:
                status = "timed_out_waiting_for_image"
                message = f"No image appeared within {timeout_seconds}s. It may still finish in {project_url or page.url}"

            try:
                credit_info = _credits_after(page, credits_before)
            except Exception:
                credit_info = {"credits_before": credits_before, "credits_after": None, "credits_used": None,
                               "flow_account": None, "flow_project_url": project_url}
            if credit_info["credits_used"] is not None:
                credits_cost = credit_info["credits_used"]
            return {
                **credit_info,
                "status": status,
                "engine": "google_flow_browser_credits",
                "mode": "image",
                "prompt": full_prompt,
                "flow_settings": flow_settings,
                "credits_cost": credits_cost,
                "approved": approved,
                "output_path": saved,
                "ui_screenshot": ui_screenshot,
                "message": message,
            }
        finally:
            context.close()
