import asyncio
import sys

import polars as pl
from loguru import logger
from playwright.sync_api import sync_playwright

logger.remove()

logger.add(sys.stderr, level="TRACE")

BASE_URL = "https://ddm.acponline.org/"


def run():
    with sync_playwright() as p:
        run_headless = False
        browser = p.chromium.launch(headless=run_headless)

        context = browser.new_context()

        page = context.new_page()

        page.goto(BASE_URL)

        logger.debug(f"started browser, {run_headless=} (should be visible now)")

        board_tag_seq = "a[href*='board']"

        game_links = page.locator(board_tag_seq).all()
        game_count = len(game_links)

        if game_count == 0:
            logger.critical(f"{game_count=}, no boards detected to scrape")
            raise RuntimeError(f"{game_count=}, no boards detected to scrape")

        logger.debug(f"Found {game_count} games on main menu.")

        scraped_data = []

        for i in range(game_count):
            # Re-query game links to avoid stale elements after navigating back
            games = page.locator(board_tag_seq).all()
            if i >= len(games):
                logger.critical(f"{game_count=}, no boards detected to scrape")
                raise RuntimeError(f"{game_count=}, no boards detected to scrape")

            game_title = games[i].inner_text()
            logger.debug(f"\nProcessing Game [{i + 1}/{game_count}]: {game_title}")

            # Click into the game grid
            games[i].click()
            logger.trace(f"Click completed to {games[i]=}.")
            page.wait_for_load_state("networkidle")

            # Query grid cells / point links
            question_buttons = page.locator(
                "a:has-text('10'), a:has-text('20'), a:has-text('30'), a:has-text('40'), a:has-text('50')"
            ).all()
            q_count = len(question_buttons)
            logger.debug(f"Found {q_count} questions in this grid.")

            for q_idx in range(q_count):
                try:
                    grid_cells = page.locator(
                        "a:has-text('10'), a:has-text('20'), a:has-text('30'), a:has-text('40'), a:has-text('50')"
                    ).all()
                    if q_idx >= len(grid_cells):
                        break

                    # Click question point value
                    grid_cells[q_idx].click()
                    page.wait_for_load_state("networkidle")

                    # TODO: doesn't catch anything (returns "NA") and shouldn't; parse the answer for it!! point and system are in it
                    category = (
                        page.locator("#category, .category-title").inner_text()
                        if page.locator("#category, .category-title").count()
                        else "N/A"
                    )
                    # TODO: doesn't catch anything (returns "NA") and shouldn't; parse the answer for it!! point and system are in it
                    points = (
                        page.locator("#points, .point-value").inner_text()
                        if page.locator("#points, .point-value").count()
                        else "N/A"
                    )

                    question_text = page.locator(
                        "#question, .question-text"
                    ).inner_text()
                    logger.trace(f"Q recovered: {question_text}")

                    # Click "Show Answer" button
                    show_answer_btn = page.locator(
                        "button:has-text('Show Answer'), input[value='Show Answer']"
                    )
                    if show_answer_btn.count() > 0:
                        show_answer_btn.click()
                        page.wait_for_timeout(300)

                    answer_text = page.locator("#answer, .answer-text").inner_text()
                    logger.trace(f"A recovered: {answer_text}")

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
                    # TODO: I don't get how this works or if it's necessary at all.
                    return_btn = page.locator(
                        "button:has-text('I was correct'), input[value='I was correct']"
                    )
                    if return_btn.count() > 0:
                        return_btn.click()
                    else:
                        page.locator(
                            "button:has-text('I was incorrect'), input[value='I was incorrect']"
                        ).click()

                    page.wait_for_load_state("networkidle")

                except Exception as e:
                    logger.error(f"  Error processing question {q_idx + 1}: {e}")
                    page.goto(BASE_URL)
                    break

            # Return to main game menu after finishing grid
            menu_btn = page.locator("a:has-text('Menu'), button:has-text('Menu')")
            if menu_btn.count() > 0:
                menu_btn.click()
            else:
                page.goto(BASE_URL)

        # Build Polars DataFrame from dicts
        df = pl.DataFrame(scraped_data)

        output_path = "doctors_dilemma_questions.csv"

        df.write_csv(output_path)
        logger.success(f"Successfully exported questions to {output_path}")

        browser.close()


asyncio.run(run())
