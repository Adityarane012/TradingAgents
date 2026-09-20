from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_global_news,
    get_instrument_context_from_state,
    get_language_instruction,
    get_macro_indicators,
    get_news,
    get_prediction_markets,
)
from tradingagents.dataflows.india_context import india_market_context
from tradingagents.dataflows.symbol_utils import is_india_ticker

# India-specific macro aliases resolve through FRED's MACRO_SERIES table
# (tradingagents/dataflows/fred.py) — sourced from IMF/OECD and slower-moving
# than the domestic US series, so the guidance below asks for the as-of date
# rather than implying central-bank-fresh data. RBI policy rates are NOT
# among them: FRED's India discount-rate series stopped updating in 2022,
# so the live repo/SDF/MSF/CRR/SLR are scraped from RBI and injected as
# part of the market context block below (see india_context).
_INDIA_NEWS_GUIDANCE = (
    "\n\nThis is an Indian (NSE/BSE) equity. In addition to the US-centric "
    "aliases above, get_macro_indicators also accepts 'india_cpi' (India CPI), "
    "'usdinr' (USD/INR rate) and 'india_10y_yield'. These IMF/OECD-sourced "
    "series update on a slower cadence than US ones — read the as-of date in "
    "the tool output and say so if the latest observation looks dated, rather "
    "than treating it as current; the tool marks a series that has fallen "
    "behind its own publication schedule. Do NOT ask it for an RBI policy "
    "rate: FRED has no live India policy-rate series, and the current repo, "
    "SDF, MSF, CRR and SLR are supplied directly in the market data below. "
    "'fed_funds_rate', 'dollar_index', and 'vix' remain relevant here too: US "
    "rate/dollar moves drive FII flows into and out of India. For "
    "get_global_news and get_news, weigh domestic catalysts you already know "
    "from general knowledge — RBI Monetary Policy Committee decisions, the "
    "Union Budget, NSE F&O expiry dynamics, and FII/DII daily flow direction "
    "— but only report them as fact when a tool result or explicit context "
    "confirms them for the current date; otherwise flag them as things to "
    "watch rather than asserting today's number."
)


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        asset_type = state.get("asset_type", "stock")
        asset_label = "company" if asset_type == "stock" else "asset"
        instrument_context = get_instrument_context_from_state(state)
        ticker = state.get("company_of_interest", "")

        tools = [
            get_news,
            get_global_news,
            get_macro_indicators,
            get_prediction_markets,
        ]

        system_message = (
            f"You are a news researcher tasked with analyzing recent news and trends over the past week. Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics. Use the available tools: get_news(ticker, start_date, end_date) for {asset_label}-specific news by ticker symbol, get_global_news(curr_date, look_back_days, limit) for broader macroeconomic news, get_macro_indicators(indicator, curr_date, look_back_days) to ground macro commentary in actual data from FRED (e.g. 'cpi', 'core_pce', 'unemployment', 'fed_funds_rate', '10y_treasury', 'yield_curve'), and get_prediction_markets(topic, limit) for live market-implied probabilities of forward-looking events (e.g. 'Fed rate cut', 'recession 2026', geopolitical or sector events). Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
            + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + (_INDIA_NEWS_GUIDANCE if is_india_ticker(ticker) else "")
            # Pre-fetched NSE market data + exchange filings (empty string for
            # non-Indian tickers or when india_data_enabled=False). These are
            # the domestic catalysts the guidance above tells the model to
            # watch for but previously gave it no way to actually observe.
            + india_market_context(ticker, current_date)
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
            "news_report": report,
        }

    return news_analyst_node
