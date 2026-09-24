import asyncio

import polars as pl
from playwright.async_api import async_playwright

BASE_URL = "https://ddm.acponline.org/"


async def run():
    async with async_playwright() as p:
        # Launch browser (set headless=False if you need to log in manually)
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(BASE_URL)
        await page.wait_for_load_state("networkidle")

        scraped_data = []

        # Find all game links on the main menu
        game_links = await page.locator("a[href*='game']").all()
        game_count = len(game_links)
        print(f"Found {game_count} games on main menu.")

        for i in range(game_count):
            # Re-query game links to avoid stale elements after navigating back
            games = await page.locator("a[href*='game']").all()
            if i >= len(games):
                break

            game_title = await games[i].inner_text()
            print(f"\nProcessing Game [{i + 1}/{game_count}]: {game_title}")

            # Click into the game grid
            await games[i].click()
            await page.wait_for_load_state("networkidle")

            # Query grid cells / point links
            question_buttons = await page.locator(
                "a:has-text('10'), a:has-text('20'), a:has-text('30'), a:has-text('40'), a:has-text('50')"
            ).all()
            q_count = len(question_buttons)
            print(f"  Found {q_count} questions in this grid.")

            for q_idx in range(q_count):
                try:
                    grid_cells = await page.locator(
                        "a:has-text('10'), a:has-text('20'), a:has-text('30'), a:has-text('40'), a:has-text('50')"
                    ).all()
                    if q_idx >= len(grid_cells):
                        break

                    # Click question point value
                    await grid_cells[q_idx].click()
                    await page.wait_for_load_state("networkidle")

                    # Extract Question details
                    category = (
                        await page.locator("#category, .category-title").inner_text()
                        if await page.locator("#category, .category-title").count()
                        else "N/A"
                    )
                    points = (
                        await page.locator("#points, .point-value").inner_text()
                        if await page.locator("#points, .point-value").count()
                        else "N/A"
                    )
                    question_text = await page.locator(
                        "#question, .question-text"
                    ).inner_text()

                    # Click "Show Answer" button
                    show_answer_btn = page.locator(
                        "button:has-text('Show Answer'), input[value='Show Answer']"
                    )
                    if await show_answer_btn.count() > 0:
                        await show_answer_btn.click()
                        await page.wait_for_timeout(300)

                    # Extract Answer text
                    answer_text = await page.locator(
                        "#answer, .answer-text"
                    ).inner_text()

                    # Append to list
                    scraped_data.append(
                        {
                            "Game": game_title.strip(),
                            "Category": category.strip(),
                            "Points": points.strip(),
                            "Question": question_text.strip(),
                            "Answer": answer_text.strip(),
                        }
                    )

                    # Click "I was correct" or "I was incorrect" to return to the grid menu
                    return_btn = page.locator(
                        "button:has-text('I was correct'), input[value='I was correct']"
                    )
                    if await return_btn.count() > 0:
                        await return_btn.click()
                    else:
                        await page.locator(
                            "button:has-text('I was incorrect'), input[value='I was incorrect']"
                        ).click()

                    await page.wait_for_load_state("networkidle")

                except Exception as e:
                    print(f"  Error processing question {q_idx + 1}: {e}")
                    await page.goto(BASE_URL)
                    break

            # Return to main game menu after finishing grid
            menu_btn = page.locator("a:has-text('Menu'), button:has-text('Menu')")
            if await menu_btn.count() > 0:
                await menu_btn.click()
            else:
                await page.goto(BASE_URL)

        # Build Polars DataFrame from dicts
        df = pl.DataFrame(scraped_data)

        # Write out to CSV (or Parquet / IPC)
        df.write_csv("doctors_dilemma_questions.csv")
        print("\nSuccessfully exported questions to 'doctors_dilemma_questions.csv'!")

        await browser.close()


asyncio.run(run())
