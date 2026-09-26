import re
import sys
from typing import Any, NamedTuple

import polars as pl
from loguru import logger
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, sync_playwright

logger.remove()
logger.add(sys.stderr, level="TRACE")

BASE_URL = "https://ddm.acponline.org/"

question_regex = (
    r"\s*Q:( |\s)?(?P<question>.*)(.|\s)*^(?P<category>[A-Z]*): (?P<point_value>\d\d)"
)
answer_regex = (
    r"\s*A:( |\s)?(?P<answer>.*)(.|\s)*^(?P<category>[A-Z]*): (?P<point_value>\d\d)"
)
# the site leaves its own placeholder in place when a question has no answer
answer_placeholder = "The answer Goes Here."
# a board is listed as "MM/YY: <categories>", e.g. "09/26: Endocrinology ..."
game_date_regex = r"(?P<month>\d{2})/(?P<year>\d{2})"

output_path = "doctors_dilemma_questions.csv"
year_tab_selector = "a.showgamelist"
visible_game_list_selector = "span.game_list:visible"
visible_board_link_selector = f"{visible_game_list_selector} a[href*='/board/']"
board_square_selector = "td[id^='board_']"
question_square_selector = "#board_{board_pos} a"
question_output_text_selector = "#question"
answer_button_selector = "#show_answer_button"
answer_text_output_selector = "#answer"
correct_button_selector = "#answer_correct"
gameover_selector = "#gameover"
clear_quiz_button_selector = "#clear_completed_quiz"
try_another_button_selector = "#completed_quiz_try_another"
run_headless = False
action_timeout_ms = 20_000
board_retry_attempts = 3

# The board ships with #mainQuiz already visible and only swaps in the real
# question data once its JSON request settles, so "is the board on screen" is
# not a usable readiness signal. quizScores[quizID] is only populated by the
# site's own init handler, which finishes drawing the board or showing the
# Game Over screen in the same synchronous block.
visibility_helper_js = """
window.is_shown = (element) => {
    if (!element) { return false; }
    if (typeof element.checkVisibility === 'function') {
        return element.checkVisibility();
    }
    return element.offsetParent !== null;
};
"""
board_ready_js = """() => {
    const initialized = window.quizScores !== undefined
        && window.quizID !== undefined
        && window.quizScores[String(window.quizID)] !== undefined;
    return initialized && (is_shown(document.getElementById('mainQuiz'))
        || is_shown(document.getElementById('gameover')));
}"""
question_ready_js = """() => {
    // the board HTML ships with the question text already filled in, so the
    // placeholder is what tells us the real text has not arrived yet
    const text = document.querySelector('#question_text');
    return is_shown(document.getElementById('question'))
        && text !== null
        && text.textContent.trim() !== ''
        && !text.textContent.includes('The Question Goes Here.');
}"""
answer_ready_js = """() => {
    const text = document.querySelector('#answer_text');
    return is_shown(document.getElementById('answer'))
        && text !== null
        && text.textContent.trim() !== '';
}"""
unanswered_square_positions_js = """(squareSelector) =>
    [...document.querySelectorAll(squareSelector)]
        .filter((square) => square.querySelector('a') !== null)
        .map((square) => square.id.replace('board_', ''))"""

csv_schema = {
    "Dilemma Game Release": pl.String,
    "Dilemma Year": pl.Int64,
    "Category": pl.String,
    "Points": pl.Int64,
    "Question": pl.String,
    "Answer": pl.String,
}


class YearTab(NamedTuple):
    label: str
    href: str


class Board(NamedTuple):
    board_id: str
    href: str
    label: str
    year: int


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=run_headless)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(action_timeout_ms)
        page.add_init_script(visibility_helper_js)

        page.goto(BASE_URL)
        page.wait_for_load_state("networkidle")
        logger.debug(f"started browser, {run_headless=}")

        year_tabs = discover_year_tabs(page)

        if not year_tabs:
            logger.critical("no tabs detected to scrape")
            raise RuntimeError(f"{len(year_tabs)=}, should be > 0")

        logger.debug(f"Found {len(year_tabs)} year tabs.")

        scraped_data: list[dict[str, Any]] = []
        scraped_board_ids: set[str] = set()
        failed_boards: list[str] = []
        tab_idx = 0

        for year_tab in reversed(year_tabs):
            tab_idx += 1
            open_year_tab(page, year_tab)
            boards = discover_boards(page)
            logger.debug(f"Found {len(boards)} visible boards for {year_tab.label}.")

            if not boards:
                logger.critical(f"{boards=}, no boards detected to scrape")
                raise RuntimeError(
                    f"no boards detected under {year_tab.label} ({year_tab.href})"
                )

            board_idx = 0
            for board in boards:
                board_idx += 1

                # a few boards are listed under two season tabs at once
                if board.board_id in scraped_board_ids:
                    logger.info(
                        f"Skipping {board.href} under {year_tab.label}, it is also "
                        f"listed under an earlier tab and was already scraped."
                    )
                    continue

                scraped_board_ids.add(board.board_id)
                logger.debug(
                    f"Processing Game [{board_idx}/{len(boards)}], part of "
                    f"{year_tab.label}: {board.label}"
                )

                rows = scrape_board_with_retries(
                    page, year_tab, board, tab_idx, board_idx, len(boards)
                )
                if rows is None:
                    failed_boards.append(board.href)
                    continue

                scraped_data.extend(rows)
                write_csv(scraped_data)
                logger.trace(f"wrote scraped_data, {len(scraped_data)=} acquired so far in case of crash")

        write_csv(scraped_data)

        df = pl.DataFrame(scraped_data, schema=csv_schema)
        logger.success(
            f"Collected {len(df)} questions from {len(scraped_board_ids)} boards."
        )
        logger.success(f"Successfully wrote {len(df)} question-answer sets to {output_path}")

        #TODO quantify weirdos and missing image ones so we can grab them

        browser.close()

        if failed_boards:
            logger.error(
                f"{len(failed_boards)} board(s) could not be scraped: {failed_boards}"
            )
            raise RuntimeError(f"failed to scrape {len(failed_boards)} board(s)")


def discover_year_tabs(page: Page) -> list[YearTab]:
    year_tabs = []
    for tab in page.locator(year_tab_selector).all():
        href = tab.get_attribute("href")
        if not href:
            continue
        year_tabs.append(YearTab(label=tab.inner_text().strip(), href=href))
    return year_tabs


def discover_boards(page: Page) -> list[Board]:
    """Read the boards of whichever year list is currently on show.

    Every year (and `Current`) lives in the same document and the tab click handler
    reveals exactly one of them, so only the visible list is read here.
    """
    boards = []
    for link in page.locator(visible_board_link_selector).all():
        href = link.get_attribute("href") or ""
        board_id = href.rsplit("/", 1)[-1]
        if not board_id.isdigit():
            continue

        label = " ".join(link.inner_text().split())
        listing = link.evaluate("el => el.closest('li').textContent")
        date_match = re.search(game_date_regex, listing)
        year = 2000 + int(date_match.group("year")) if date_match else 0

        boards.append(Board(board_id=board_id, href=href, label=label, year=year))
    return boards


def open_year_tab(page: Page, year_tab: YearTab) -> None:
    """Return to the index and open the requested season list.

    Each board is opened from a freshly rendered season list on purpose. The
    old code captured the board links once as positions in the visible list and
    then reused them after the Game Over screen had sent the browser back to
    the index, where a *different* season list is the one on show. That walked
    the scraper onto a board it had already played, which is what produced the
    end-of-game screen.
    """
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")
    page.locator(f"{year_tab_selector}[href='{year_tab.href}']").click()
    page.locator(visible_board_link_selector).first.wait_for(state="visible")


def open_board(page: Page, year_tab: YearTab, board: Board) -> None:
    open_year_tab(page, year_tab)
    board_link = page.locator(f"{visible_game_list_selector} a[href='{board.href}']")
    board_link.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_function(board_ready_js)


def unanswered_square_positions(page: Page) -> list[str]:
    return page.evaluate(unanswered_square_positions_js, board_square_selector)


def reset_board_progress(page: Page, board: Board) -> None:
    """Wipe the saved score for a board so its squares become playable again.

    Progress is kept in localStorage per board, and a board that has been
    played through renders the Game Over screen instead of its grid. Any board
    left over from this run or an earlier one has to be cleared before it can
    be replayed.
    """
    logger.info(
        f"{board.href} still has saved progress, clearing it to replay the board."
    )
    clear_button = page.locator(clear_quiz_button_selector)
    if clear_button.count() == 0:
        raise RuntimeError(f"{board.href} is not playable and cannot be cleared")

    clear_button.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_function(board_ready_js)


def open_board_positions(page: Page, board: Board) -> list[str]:
    """Return the board positions still answerable, clearing progress if needed."""
    positions = unanswered_square_positions(page)
    total_squares = page.locator(board_square_selector).count()

    if page.locator(gameover_selector).is_visible() or len(positions) < total_squares:
        reset_board_progress(page, board)
        positions = unanswered_square_positions(page)
        total_squares = page.locator(board_square_selector).count()

    if not positions:
        raise RuntimeError(f"{board.href} has no questions to scrape")
    if len(positions) < total_squares:
        raise RuntimeError(
            f"{board.href} only exposes {len(positions)}/{total_squares} questions"
        )
    return positions


def scrape_board_with_retries(
    page: Page,
    year_tab: YearTab,
    board: Board,
    tab_idx: int,
    board_idx: int,
    board_count: int,
) -> list[dict[str, Any]] | None:
    for attempt in range(1, board_retry_attempts + 1):
        try:
            return scrape_board(page, year_tab, board, tab_idx, board_idx, board_count)
        except PlaywrightError as err:
            logger.warning(
                f"Attempt {attempt}/{board_retry_attempts} on {board.href} failed: {err}"
            )
            if page.is_closed():
                logger.critical("browser went away, cannot continue scraping")
                return None
            try:
                return_to_index(page)
            except PlaywrightError:
                pass
        except Exception as err:
            # one unplayable board must not sink the rest of the collection
            logger.opt(exception=True).error(
                f"Attempt {attempt}/{board_retry_attempts} on {board.href} errored: {err}"
            )
            break

    logger.error(
        f"Giving up on board {board_idx}/{board_count} ({board.href}) under "
        f"tab {tab_idx} ({year_tab.label})"
    )
    return None


def scrape_board(
    page: Page,
    year_tab: YearTab,
    board: Board,
    tab_idx: int,
    board_idx: int,
    board_count: int,
) -> list[dict[str, Any]]:
    open_board(page, year_tab, board)
    board_positions = open_board_positions(page, board)
    initial_q_count = len(board_positions)

    logger.debug(f"Found {initial_q_count} questions in {board.href} at start.")

    rows: list[dict[str, Any]] = []
    q_idx = 0

    for this_board_pos in board_positions:
        q_idx += 1# TODO fix
        parsed_dict = extract_question_and_answer(
            initial_q_count, page, q_idx, board, this_board_pos
        )

        this_question_data = {
            "Dilemma Game Release": board.label,
            "Dilemma Year": board.year,
            "Category": parsed_dict["category"],
            "Points": parsed_dict["point_value"],
            "Question": parsed_dict["question"],
            "Answer": parsed_dict["answer"],
        }
        rows.append(this_question_data)

        logger.info( #TODO info
            f"Completed question {q_idx}/{initial_q_count}, collected "
            f"{len(rows)} questions from this board."
        )
        logger.debug(f"Question contents: {this_question_data=}")
        logger.trace(
            f"Question coordinates: tab {tab_idx} ({year_tab.label}) | "
            f"board {board_idx}/{board_count} ({board.label}) | question {q_idx}"
        )

        page.locator(correct_button_selector).click()
        page.wait_for_function(board_ready_js)

    return rows


def return_to_index(page: Page) -> None:
    if page.locator(try_another_button_selector).count() > 0:
        page.locator(try_another_button_selector).click()
    else:
        page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")


def page_has_published_answer(page: Page, board_pos: str) -> bool:
    """Ask the page whether this question is supposed to have an answer.

    A handful of questions ship have no answer, and the site's own reveal
    handler then does ``.html(undefined)`` on the answer element, which jQuery
    treats as a read rather than a write. The previous question's answer is
    then still sitting on screen and would be scraped as if it were this one.
    """
    return bool(
        page.evaluate(
            "(pos) => {"
            " const question = window.quizData.questions[pos];"
            " return question !== undefined && Boolean(question.answer);"
            "}",
            board_pos,
        )
    )


def extract_question_and_answer(
    initial_q_count: int,
    page: Page,
    q_idx: int,
    board: Board,
    this_board_pos: str,
) -> dict[str, str | Any]:
    logger.trace(
        f"Clicking question {q_idx}/{initial_q_count} at position {this_board_pos}"
    )
    this_question: Locator = page.locator(
        question_square_selector.format(board_pos=this_board_pos)
    )
    this_question.click()
    page.wait_for_function(question_ready_js)

    question_text = page.locator(question_output_text_selector).inner_text()
    question_match = re.search(question_regex, question_text, re.MULTILINE)
    if not question_match:
        raise RuntimeError(f"{question_text=}, should match regex")
    parsed_dict = question_match.groupdict()
    logger.trace(f"{parsed_dict=}")

    show_answer_btn = page.locator(answer_button_selector)
    if show_answer_btn.count() > 0:
        show_answer_btn.click()

    if not page_has_published_answer(page, this_board_pos):
        logger.warning(
            f"{board.href} position {this_board_pos} has no published answer, "
            f"recording it as blank"
        )
        # both panels are labeled from the same board position, so the
        # category and points parsed off the question still hold
        parsed_dict["answer"] = ""
        return parsed_dict

    page.wait_for_function(answer_ready_js)

    answer_text = page.locator(answer_text_output_selector).inner_text()
    answer_match = re.search(answer_regex, answer_text, re.MULTILINE)
    if not answer_match:
        raise RuntimeError(f"{answer_text=}, should match regex")
    answer_parsed_dict = answer_match.groupdict()

    if answer_parsed_dict["answer"].strip() == answer_placeholder:
        logger.warning(
            f"{board.href} position {this_board_pos} still shows the site "
            f"placeholder, recording it as blank"
        )
        parsed_dict["answer"] = ""
    else:
        logger.trace(f"{answer_parsed_dict=}")
        parsed_dict["answer"] = answer_parsed_dict["answer"]

    return parsed_dict


def write_csv(scraped_data: list[dict[str, Any]]) -> None:
    pl.DataFrame(scraped_data, schema=csv_schema).write_csv(output_path)


if __name__ == "__main__":
    run()
