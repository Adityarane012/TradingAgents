from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.dataflows.india_context import india_ownership_context
from tradingagents.dataflows.symbol_utils import is_india_ticker

# Promoter shareholding used to be listed here as simply unavailable. It is
# now fetched from NSE filings and injected below (india_context), so this
# note no longer claims it is missing — telling the model data is absent when
# the prompt contains it is its own kind of wrong. What remains genuinely
# unavailable is promoter *pledging* (no free source found) and the FII/DII
# split within public holding (NSE's shareholding-pattern endpoint reports
# only promoter vs public). Those are still named as unavailable rather than
# demanded, because a prompt that insists on a number the model cannot see
# invites it to invent one.
_INDIA_FUNDAMENTALS_NOTE = (
    "\n\nThis is an Indian (NSE/BSE) equity. Three notes: (1) promoter "
    "shareholding is provided below from NSE filings — use it, and do not "
    "substitute any insider-holding percentage from the fundamentals tools, "
    "which models Indian promoter holding inaccurately. (2) Promoter share "
    "*pledging* is NOT available from any source wired up here, and the "
    "FII/DII split within public holding is present only if a section below "
    "supplies it; where a figure is not given to you, say plainly that the "
    "data was not available rather than estimating or inventing one. "
    "(3) Benchmark valuation multiples (P/E, P/B, "
    "EV/EBITDA) against Nifty 50 / domestic sector peers, not S&P 500 norms — "
    "Indian equities historically trade at different multiples than US peers."
)


def create_fundamentals_analyst(llm):
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)
        ticker = state.get("company_of_interest", "")

        tools = [
            get_fundamentals,
            get_balance_sheet,
            get_cashflow,
            get_income_statement,
        ]

        system_message = (
            "You are a researcher tasked with analyzing fundamental information over the past week about a company. Please write a comprehensive report of the company's fundamental information such as financial documents, company profile, basic company financials, and company financial history to gain a full view of the company's fundamental information to inform traders. Make sure to include as much detail as possible. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
            + " Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."
            + " Use the available tools: `get_fundamentals` for comprehensive company analysis, `get_balance_sheet`, `get_cashflow`, and `get_income_statement` for specific financial statements."
            + (_INDIA_FUNDAMENTALS_NOTE if is_india_ticker(ticker) else "")
            # Pre-fetched NSE filings (empty string for non-Indian tickers or
            # when india_data_enabled=False). Appended after the note above so
            # the model reads the caveats before the data they apply to.
            + india_ownership_context(ticker, current_date)
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "fundamentals_report": report,
        }

    return fundamentals_analyst_node
