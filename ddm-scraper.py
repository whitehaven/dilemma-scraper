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
visible_board_tag_selector = "a[href*='board']:visible"
question_selector = "a:has-text('10'), a:has-text('20'), a:has-text('30'), a:has-text('40'), a:has-text('50')"
question_output_text_selector = "#question, .question-text"
answer_button_selector = "button:has-text('Show Answer'), input[value='Show Answer']"
answer_text_output_selector = "#answer, .answer-text"
start_over_button_selector = "a:has-text('Try'), button:has-text('Try')"
incorrect_button_selector = (
    "button:has-text('I was incorrect'), input[value='I was incorrect']"
)
correct_button_selector = (
    "button:has-text('I was correct'), input[value='I was correct']"
)


def run():
    # TODO gets stuck reliably on board 2 of 2025, gets a completion screen and wants to find more questions; maybe reassignment because should only have one query ever
    with sync_playwright() as p:
        run_headless = False
        browser = p.chromium.launch(headless=run_headless)
        context = browser.new_context()
        page = context.new_page()

        page.goto(BASE_URL)
        page.wait_for_load_state("networkidle")
        logger.debug(f"started browser, {run_headless=}")

        year_tabs = page.locator(year_tab_selector).all()
        initial_tab_count = len(year_tabs)

        if initial_tab_count == 0:
            logger.critical(f"{initial_tab_count=}, no tabs detected to scrape")
            raise RuntimeError(f"{len(year_tabs)=}, should be > 0")

        logger.debug(f"Found {initial_tab_count} year tabs.")

        tab_idx = 0
        scraped_data = []
        while year_tabs:
            this_tab = year_tabs.pop()
            this_tab_label = this_tab.inner_text()
            logger.debug(f"Navigating to Year Tab: {this_tab_label}")

            this_tab.click()
            page.wait_for_load_state("networkidle")

            visible_boards = page.locator(visible_board_tag_selector).all()
            initial_visible_board_count = len(visible_boards)
            logger.debug(
                f"Found {initial_visible_board_count} visible boards for {this_tab_label}."
            )
            if len(visible_boards) == 0:
                logger.critical(f"{visible_boards=}, no boards detected to scrape")
                raise RuntimeError(
                    f"{initial_visible_board_count=}, no boards detected to scrape"
                )

            board_idx = 0
            while visible_boards:
                this_board = visible_boards.pop()
                this_board_label = this_board.inner_text()
                logger.debug(
                    f"Processing Game [{board_idx + 1}/{initial_visible_board_count}], part of {this_tab_label}: {this_board_label}"
                )

                this_board.click()
                page.wait_for_timeout(300)
                page.wait_for_load_state("networkidle")

                question_buttons = page.locator(question_selector).all()
                initial_q_count = len(question_buttons)

                if initial_q_count == 0:
                    logger.critical(
                        f"{initial_q_count=}, no initial questions detected to scrape"
                    )
                    raise RuntimeError(
                        f"{initial_q_count=}, no initial questions detected to scrape"
                    )

                logger.debug(
                    f"Found {initial_q_count} questions in this grid at start."
                )

                q_idx = 0
                while question_buttons:
                    this_question = question_buttons.pop()

                    logger.trace(f"Clicking question {q_idx + 1}/{initial_q_count}")
                    this_question.click()
                    page.wait_for_load_state("networkidle")

                    question_text = page.locator(
                        question_output_text_selector
                    ).inner_text()
                    question_match = re.search(
                        question_regex, question_text, re.MULTILINE
                    )
                    if not question_match:
                        raise RuntimeError(f"{question_text=}, should match regex")
                    question_parsed_dict = question_match.groupdict()
                    logger.trace(f"{question_parsed_dict=}")

                    show_answer_btn = page.locator(answer_button_selector)
                    if show_answer_btn.count() > 0:
                        show_answer_btn.click()
                        page.wait_for_timeout(300)

                    answer_text = page.locator(answer_text_output_selector).inner_text()
                    answer_match = re.search(answer_regex, answer_text, re.MULTILINE)
                    if not answer_match:
                        raise RuntimeError(f"{answer_text=}, should match regex")
                    answer_parsed_dict = answer_match.groupdict()
                    logger.trace(f"{answer_parsed_dict=}")

                    this_question_data = {
                        "Dilemma Game Release": this_board_label.strip(),
                        "Dilemma Year": this_tab_label,
                        "Category": question_parsed_dict["category"],
                        "Points": question_parsed_dict["point_value"],
                        "Question": question_parsed_dict["question"],
                        "Answer": answer_parsed_dict["answer"],
                    }
                    scraped_data.append(this_question_data)
                    logger.success(
                        f"Completed question {q_idx + 1}/{initial_q_count}, appended to collection of {len(scraped_data)} questions."
                    )
                    logger.trace(f"Completed question contents: {this_question_data=}")
                    logger.trace(
                        f"Question coordinates: tab {tab_idx + 1} ({this_tab_label}) | board {board_idx + 1}"
                        f"({this_board_label}) | question {q_idx + 1}"
                    )

                    q_idx += 1

                    return_btn = page.locator(correct_button_selector)

                    if return_btn.count() == 0:
                        error_output = (
                            f"Return button not found after Q @ tab {tab_idx + 1} ({this_tab_label})"
                            f"| board {board_idx + 1} ({this_board_label})"
                            f"| question {q_idx + 1}"
                        )
                        logger.critical(error_output)
                        raise RuntimeError(error_output)

                    return_btn.click()
                    page.wait_for_load_state("networkidle")

                start_over_button = page.locator(start_over_button_selector)
                assert start_over_button.count() > 0
                start_over_button.click()
                page.wait_for_load_state("networkidle")

                board_idx += 1
            tab_idx += 1

        df = pl.DataFrame(scraped_data)
        logger.success(f"{df.glimpse}")

        df.write_csv(output_path)
        logger.success(f"Successfully exported questions and answers to {output_path}")

        browser.close()


if __name__ == "__main__":
    run()
