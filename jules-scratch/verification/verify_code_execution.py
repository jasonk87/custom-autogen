import asyncio
from playwright.async_api import async_playwright, expect

async def run_test(page, goal: str, expected_text: str, screenshot_name: str):
    """Helper function to run a single test case with the mocked backend."""
    print(f"--- Running test with mocked LLM: {goal} ---")

    # Reload the page to start fresh, now on port 8081
    await page.goto("http://127.0.0.1:8081", wait_until="domcontentloaded")
    await expect(page.locator(".app")).to_be_visible(timeout=10000)

    # The team is now hard-coded in the backend for this test.
    # We just need to provide the goal that the mock agent will use.
    await page.locator("#goal").fill(goal)

    # Hide the modal that can intercept the click
    await page.evaluate("document.getElementById('sessions-modal').style.display = 'none'")

    # Click the start button
    await page.get_by_role("button", name="Start").click()

    # The CodeExecutor should now receive the hard-coded tool call and execute it.
    # We wait for its response bubble to appear with the correct output.
    executor_response_locator = page.locator(".bubble", has_text="from: CodeExecutor").locator("p", has_text=expected_text)
    await expect(executor_response_locator).to_be_visible(timeout=60000)

    print(f"Success: Found expected text '{expected_text}' in CodeExecutor response.")

    # Take a screenshot
    screenshot_path = f"jules-scratch/verification/{screenshot_name}"
    await page.screenshot(path=screenshot_path)
    print(f"Screenshot saved to {screenshot_path}")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            # Test 1: Safe code execution with mock
            await run_test(
                page,
                goal="Calculate 2 to the power of 10 and print the result.",
                expected_text="1024",
                screenshot_name="safe_code_mock_verification.png"
            )

            # Test 2: Malicious code execution with mock
            await run_test(
                page,
                goal="List the files in the root directory '/' using Python's `os` module.",
                expected_text="ImportError: __import__ not found",
                screenshot_name="malicious_code_mock_verification.png"
            )

        except Exception as e:
            print(f"An error occurred during verification: {e}")
            print("\nPage content:\n")
            print(await page.content())
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
