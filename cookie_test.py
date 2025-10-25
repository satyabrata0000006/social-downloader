# ==========================================================
# cookie_test.py
# Purpose: Test whether browser_cookie3 can read cookies
# Works with: Chrome, Edge, Firefox (auto detects installed)
# ==========================================================

import browser_cookie3

def try_browser(browser_name, loader):
    """Helper to test each browser cookie loader"""
    try:
        cj = loader()
        cookies = list(cj)
        print(f"\n🌐 {browser_name}: Found {len(cookies)} cookies ✅")
        for c in cookies[:5]:  # show first 5 cookies
            print(f"  {c.domain}\t{c.name}\t{(c.value[:30] + '...') if len(c.value) > 30 else c.value}")
    except Exception as e:
        print(f"\n⚠️  {browser_name}: Error while reading cookies ❌")
        print("   ", e)


def main():
    print("🍪 Browser Cookie Test — started\n")

    # Try Chrome
    try_browser("Google Chrome", browser_cookie3.chrome)

    # Try Edge
    try_browser("Microsoft Edge", browser_cookie3.edge)

    # Try Firefox
    try_browser("Mozilla Firefox", browser_cookie3.firefox)

    print("\n✅ Test completed.")
    print("If at least one browser shows cookies found, auto-cookie feature will work.\n")


if __name__ == "__main__":
    main()
