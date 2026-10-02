#!/usr/bin/env python3
"""Check the web UI at phone widths: horizontal overflow and script errors.

    python tools/mobile_check.py                    # measure every tab
    python tools/mobile_check.py --shots out/       # and save screenshots
    python tools/mobile_check.py --url http://localhost:8766

Overflow is the failure this catches: a card wider than the viewport pushes
the whole page sideways and the content on its right cannot be reached. Each
tab is opened at iPhone SE, 15 and Pro Max widths in light and dark, and any
element sticking out past the viewport is reported, along with console errors.

Needs Playwright (`pip install playwright`) and a running server; exits 0 if
there is nothing to report.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

WIDTHS = (375, 390, 430)
TABS = ("plan", "shop", "recipes", "pantry", "settings")

PROBE = """() => {
  const vw = document.documentElement.clientWidth, out = [];
  document.querySelectorAll('body *').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width && r.right > vw + 1 && getComputedStyle(el).position !== 'fixed') {
      const cls = typeof el.className === 'string' && el.className.trim()
        ? '.' + el.className.trim().split(/\\s+/).join('.') : '';
      out.push(el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + cls + ' right=' + Math.round(r.right));
    }
  });
  return {vw, scroll: document.documentElement.scrollWidth, overflow: out.slice(0, 12)};
}"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8766")
    ap.add_argument("--shots", type=Path, help="directory for screenshots")
    ap.add_argument("--executable", help="Chromium binary, if Playwright's own is not installed")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed: pip install playwright", file=sys.stderr)
        return 2

    problems: list[str] = []
    if args.shots:
        args.shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        launch = {"executable_path": args.executable} if args.executable else {}
        browser = pw.chromium.launch(**launch)
        for scheme in ("light", "dark"):
            for width in WIDTHS:
                ctx = browser.new_context(viewport={"width": width, "height": 844},
                                          device_scale_factor=2, color_scheme=scheme,
                                          is_mobile=True, has_touch=True)
                page = ctx.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                page.goto(args.url, wait_until="networkidle")
                for tab in TABS:
                    try:
                        page.click(f'nav.tabs button[data-tab="{tab}"]', timeout=5000)
                    except Exception:
                        # What overflow does on a phone: the page zooms out to
                        # fit, and the fixed tab bar ends up under the content.
                        res = page.evaluate(PROBE)
                        problems.append(f"{scheme} {width}px: the {tab} tab could not be tapped "
                                        f"(scrollWidth {res['scroll']} > {res['vw']}): "
                                        + "; ".join(res["overflow"]))
                        break
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(250)
                    res = page.evaluate(PROBE)
                    if res["scroll"] > res["vw"] + 1 or res["overflow"]:
                        problems.append(f"{scheme} {width}px {tab}: scrollWidth {res['scroll']} > "
                                        f"{res['vw']}: " + "; ".join(res["overflow"]))
                    if args.shots and width == 390:
                        page.screenshot(path=str(args.shots / f"{tab}-{scheme}.png"), full_page=True)
                problems += [f"{scheme} {width}px console: {e}" for e in errors]
                ctx.close()
        browser.close()

    for p in problems:
        print(p)
    print("ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
