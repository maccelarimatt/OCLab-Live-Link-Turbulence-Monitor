"""Optional end-to-end test: python tests/browser_smoke.py [--browser /path/to/chrome]."""
import argparse
import csv
import io
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", help="Optional existing Chromium/Chrome executable")
    args = parser.parse_args()
    output = Path("test-results")
    output.mkdir(exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="oclab-browser-") as directory:
        config = Path(directory) / "config.toml"
        config.write_text(f'mode="demo"\ndata_dir="{directory}/data"\nminimum_free_disk_mb=0\ncapture_interval_s=0.1\nport={port}\n')
        with (output / "browser-server.log").open("w") as log:
            server = subprocess.Popen([sys.executable, "-m", "oclab", "--config", str(config)], stdout=log, stderr=log)
            try:
                for _ in range(100):
                    try:
                        if httpx.get(url + "/api/health", timeout=1).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if server.poll() is not None:
                        raise AssertionError("Server failed; inspect test-results/browser-server.log")
                    time.sleep(0.1)
                else:
                    raise AssertionError("Server did not start")
                with sync_playwright() as p:
                    browser = p.chromium.launch(executable_path=args.browser, headless=True)
                    context = browser.new_context(viewport={"width": 1440, "height": 1080})
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(url)
                    expect(page.locator("#connection-label")).to_have_text("Demo running")
                    expect(page.locator("#mode-banner")).to_be_visible()
                    expect(page.locator("#x-value")).not_to_have_text("—")
                    expect(page.locator("#signal-empty")).not_to_be_visible()
                    page.locator("#waveform-tab").click()
                    expect(page.locator("#waveform-tab")).to_have_attribute("aria-pressed", "true")
                    expect(page.locator("#chart-count")).to_have_text("256 preview points")
                    page.locator("#trend-tab").click()
                    page.locator("#time-window").select_option("60")
                    expect(page.locator("#sessions-body .download-links")).to_have_count(1)
                    with page.expect_download() as download_info:
                        page.locator('a[href$="/summary.csv"]').click()
                    download = download_info.value
                    rows = list(csv.DictReader(io.StringIO(Path(download.path()).read_text())))
                    assert rows and rows[0]["source"] == "demo"
                    with page.expect_download() as metadata_download:
                        page.locator('a[href$="/metadata.json"]').click()
                    assert metadata_download.value.suggested_filename.endswith("metadata.json")
                    page.evaluate("() => window.scrollTo(0,0)")
                    page.screenshot(path=str(output / "dashboard-desktop.png"), full_page=True)
                    assert not page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth")
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.screenshot(path=str(output / "dashboard-mobile.png"), full_page=True)
                    assert not page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth")
                    # Browser offline emulation can leave an existing localhost
                    # event stream open. Actually drop the service connection.
                    server.kill()
                    server.wait(timeout=10)
                    expect(page.locator("#connection-label")).to_have_text("Connection lost", timeout=15000)
                    server = subprocess.Popen([sys.executable, "-m", "oclab", "--config", str(config)], stdout=log, stderr=log)
                    expect(page.locator("#connection-label")).to_have_text("Demo running", timeout=20000)
                    assert not errors, errors
                    browser.close()
                    print("Browser checks passed: live data, chart controls, CSV/metadata downloads, desktop/mobile layout, offline/reconnect, no JavaScript errors.")
            finally:
                server.terminate()
                server.wait(timeout=40)


if __name__ == "__main__":
    main()
