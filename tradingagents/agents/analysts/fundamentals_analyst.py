from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.dataflows.symbol_utils import is_india_ticker

# yfinance's fundamentals/balance-sheet/cashflow tools have no field for
# promoter shareholding, promoter pledging, or FII/DII ownership — all
# material governance signals for Indian equities but simply absent from the
# data this analyst can call. Telling the model to "always analyze" them
# (as opposed to noting they're unavailable) would just invite it to
# fabricate numbers under prompt pressure, the same failure mode the
# sentiment analyst was redesigned to avoid (see sentiment_analyst.py).
_INDIA_FUNDAMENTALS_NOTE = (
    "\n\nThis is an Indian (NSE/BSE) equity. Two notes: (1) the available "
    "tools have no field for promoter shareholding %, promoter share "
    "pledging, or FII/DII ownership changes — material governance signals "
    "for Indian companies that you cannot verify here. If they're relevant, "
    "say plainly that this data was not available rather than estimating or "
    "inventing a figure. (2) Benchmark valuation multiples (P/E, P/B, "
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
