from playwright.sync_api import sync_playwright

def run(playwright):
    browser = playwright.chromium.launch()
    page = browser.new_page()
    page.goto("http://127.0.0.1:8080")

    # Click on the settings tab and take a screenshot
    page.click("text=Settings")
    page.screenshot(path="jules-scratch/verification/settings_tab.png")

    # Click on the setup tab and take a screenshot
    page.click("text=Setup")
    page.screenshot(path="jules-scratch/verification/setup_tab.png")

    browser.close()

with sync_playwright() as playwright:
    run(playwright)
