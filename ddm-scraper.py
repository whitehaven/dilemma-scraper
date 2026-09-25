import re
import sys

import polars as pl
from loguru import logger
from playwright.sync_api import sync_playwright

logger.remove()

logger.add(sys.stderr, level="TRACE")

BASE_URL = "https://ddm.acponline.org/"

question_regex = (
    r"\s*Q:( |\s)?(?P<question>.*)(.|\s)*^(?P<category>[A-Z]*): (?P<point_value>\d\d)"
)
answer_regex = (
    r"\s*A:( |\s)?(?P<answer>.*)(.|\s)*^(?P<category>[A-Z]*): (?P<point_value>\d\d)"
)
output_path = "doctors_dilemma_questions.csv"
year_tab_selector = "a:has-text('20'), button:has-text('20'), a:has-text('Current')"
board_tag_selector = "a[href*='board']:visible"
question_selector = "a:has-text('10'), a:has-text('20'), a:has-text('30'), a:has-text('40'), a:has-text('50')"


def run():

    with sync_playwright() as p:
        run_headless = False
        browser = p.chromium.launch(headless=run_headless)

        context = browser.new_context()

        page = context.new_page()

        page.goto(BASE_URL)

        logger.debug(f"started browser, {run_headless=} (should be visible now)")

        game_links = page.locator(board_tag_selector).all()
        game_count = len(game_links)

        if game_count == 0:
            logger.critical(f"{game_count=}, no boards detected to scrape")
            raise RuntimeError(f"{game_count=}, no boards detected to scrape")

        logger.debug(f"Found {game_count} games on main menu.")

        scraped_data = []

        year_tabs = page.locator(year_tab_selector).all()

        logger.debug(f"Found {len(year_tabs)} year tabs.")
        logger.trace(f"{len(year_tabs)} tabs found, namely: {year_tabs=}")

        if len(year_tabs) == 0:
            raise RuntimeError(f"{len(year_tabs)=}, should be > 0")

        for tab_idx in range(len(year_tabs)):
            tabs = page.locator(year_tab_selector).all()
            year_label = tabs[tab_idx].inner_text()
            logger.debug(f"Navigating to Year Tab: {year_label}")

            tabs[tab_idx].click()
            page.wait_for_load_state("networkidle")

            visible_boards = page.locator(board_tag_selector).all()
            board_count = len(visible_boards)
            logger.debug(f"Found {board_count} visible boards for {year_label}.")

            for i in range(len(visible_boards)):
                # Re-query game links to avoid stale elements after navigating back
                # visible_boards = page.locator(board_tag_selector).all()
                if i >= len(visible_boards):
                    logger.critical(f"{visible_boards=}, no boards detected to scrape")
                    raise RuntimeError(f"{board_count=}, no boards detected to scrape")

                game_title = visible_boards[i].inner_text()
                logger.debug(f"\nProcessing Game [{i + 1}/{game_count}]: {game_title}")

                # Click into the game grid
                visible_boards[i].click()
                page.wait_for_load_state("networkidle")

                # Query grid cells / point links
                question_buttons = page.locator(question_selector).all()
                q_count = len(question_buttons)
                logger.debug(f"Found {q_count} questions in this grid.")

                # TODO: when runs off page to next year, can't find even though is listed (would be reachable through year links following Show:)
                
                grid_cells = page.locator(question_selector).all()
                
                for q_idx in range(q_count):
                    try:
                        
                        # if q_idx >= len(grid_cells):
                            # break

                        # Click question point value
                        grid_cells[q_idx].click()
                        page.wait_for_load_state("networkidle")

                        question_text = page.locator(
                            "#question, .question-text"
                        ).inner_text()

                        question_match = re.search(
                            question_regex, question_text, re.MULTILINE
                        )

                        if not question_match:
                            raise RuntimeError(f"{question_text=}, should match regex")

                        question_parsed_dict = question_match.groupdict()

                        logger.trace(f"{question_parsed_dict=}")

                        # Click "Show Answer" button
                        show_answer_btn = page.locator(
                            "button:has-text('Show Answer'), input[value='Show Answer']"
                        )
                        if show_answer_btn.count() > 0:
                            show_answer_btn.click()
                            page.wait_for_timeout(300)

                        answer_text = page.locator("#answer, .answer-text").inner_text()

                        answer_match = re.search(
                            answer_regex, answer_text, re.MULTILINE
                        )

                        if not answer_match:
                            raise RuntimeError(f"{answer_text=}, should match regex")

                        answer_parsed_dict = answer_match.groupdict()

                        logger.trace(f"{answer_parsed_dict=}")

                        this_question_data = {
                            "Dilemma Game Release": game_title.strip(),
                            "Dilemma Year": year_label,
                            "Category": question_parsed_dict["category"],
                            "Points": question_parsed_dict["point_value"],
                            "Question": question_parsed_dict["question"],
                            "Answer": answer_parsed_dict["answer"],
                        }
                        logger.debug(f"{this_question_data}") #TODO: describe better
                        scraped_data.append(this_question_data)

                        # Click "I was correct" or "I was incorrect" to return to the grid menu
                        # TODO: I don't get how this works or if it's necessary at all. I don't get the logic. It seems like it goes back and forth answering one button or the other. Maybe based on load order? I don't know. Odd.
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

                    # TODO: also don't know what this is for, probably remove if works
                    except Exception as e:
                        logger.error(f"  Error processing question {q_idx + 1}: {e}")
                        raise RuntimeError
                        page.goto(BASE_URL)
                        break

            # Return to main game menu after finishing grid
            menu_btn = page.locator("a:has-text('Menu'), button:has-text('Menu')")
            if menu_btn.count() > 0:
                menu_btn.click()
            else:
                page.goto(BASE_URL)

        df = pl.DataFrame(scraped_data)
        logger.success(f"{df.glimpse}")

        df.write_csv(output_path)
        logger.success(f"Successfully exported questions and answers to {output_path}")

        browser.close()


if __name__ == "__main__":
    run()
