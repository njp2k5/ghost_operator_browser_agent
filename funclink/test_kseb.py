import asyncio


async def test():
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    b = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    ctx = await b.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        ignore_https_errors=True,
    )
    await ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    p = await ctx.new_page()

    for url in ["https://wss.kseb.in", "https://www.kseb.in"]:
        print(f"\n--- Testing: {url} ---")
        try:
            resp = await p.goto(url, wait_until="domcontentloaded", timeout=15000)
            print(f"  Response status: {resp.status if resp else 'no resp'}")
        except Exception as e:
            print(f"  Navigation error: {e}")

        await p.wait_for_timeout(2000)
        title = await p.title()
        final_url = p.url
        print(f"  Final URL: {final_url}")
        print(f"  Page title: {title}")

        content = await p.content()
        print(f"  HTML length: {len(content)} chars")
        print(f"  Has <body>: {'<body' in content.lower()}")

        ss = await p.screenshot(type="png")
        fname = f"test_kseb_{url.split('//')[1].split('.')[0]}.png"
        with open(fname, "wb") as f:
            f.write(ss)
        print(f"  Screenshot: {len(ss)} bytes → saved {fname}")

    await b.close()
    await pw.stop()


asyncio.run(test())
