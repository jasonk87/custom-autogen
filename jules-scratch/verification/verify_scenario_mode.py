from playwright.sync_api import sync_playwright, expect

def run_verification():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto("http://127.0.0.1:8080")
        page.wait_for_load_state('networkidle')

        # Verify that the number of agents input is visible
        num_agents_input = page.locator("#num-agents")
        expect(num_agents_input).to_be_visible()

        # Take a screenshot
        page.screenshot(path="jules-scratch/verification/scenario_mode_verification.png")

        browser.close()

if __name__ == "__main__":
    run_verification()
