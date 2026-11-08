from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.analysts.toolset import guidance, offered
from tradingagents.agents.analysts.turn import take_turn
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.tools import (
    get_indicators,
    get_market_context,
    get_market_statistics,
    get_stock_data,
    get_verified_market_snapshot,
)

# The tools this analyst is offered; its tool node is built from the same tuple.
TOOLS = offered((
    get_stock_data,
    get_indicators,
    get_verified_market_snapshot,
    get_market_context,
    get_market_statistics,
))

# Prompt paragraphs for the tools that can be switched off (TRADINGAGENTS_DISABLED_TOOLS); a tool that is off is
# not mentioned. Paragraphs are added in this order.
_GUIDANCE = {
    "get_market_context": " Also call `get_market_context` once. It gives the state of the broad market and the stock's sector (drawdowns from their 52-week highs, the VIX) and how this stock moves against the market: its volatility compared with the market's, its beta with the R-squared, its stock-specific volatility, and how much of its last quarter's move the market explains. Rules for market context: state each index's drawdown with the label the tool gives; treat volatility as a fact to be judged against the market, not as a verdict, and say that when market-wide volatility is high a stock's volatility that simply follows the market says little; separate the part of a move the market explains from the stock-specific residual, saying what drove the recent move only as the tool's 'Reading' line says it (a stock whose volatility is mostly its own can still have a recent move that is mostly what the market implies, and the reverse), and when the tool says the beta is imprecise, say so and treat the market-implied move as rough; give the relative strength against the S&P 500 and the sector as the tool states it; include every item in the tool's 'Flags to state in the report' section (or say why one does not apply); and do not call a price decline a bargain or a trap, because that judgment needs the company's finances and belongs to the later stages.",
    "get_market_statistics": " Use `get_market_statistics` for how much the stock moves and how it trades. Rules for market statistics: say these are Yahoo figures (the price-based ones as of the last close; the option-based ones come from Yahoo's option chains and can be missing or noisy for thinly traded options); describe implied volatility as the options market's annualized volatility for 30 days and compare it with realized volatility only as the tool does; call the percentile figure a proxy, as the tool does; give the one-standard-deviation ranges and the straddle-implied move as arithmetic from option prices, never as a forecast, a price target or a stop level; describe a put/call ratio as a fact about open contracts, not as a prediction; include every item in the tool's 'Flags to state in the report' section (or say why one does not apply); if the tool says the option figures are withheld or unavailable, say so and do not estimate them; and report volatility as a fact about the stock, not as a reason to buy or sell on its own.",
}


def create_market_analyst(llm):

    def market_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        system_message = (
            """You are a trading assistant tasked with analyzing financial markets. Your role is to select the **most relevant indicators** for a given market condition or trading strategy from the following list. The goal is to choose up to **8 indicators** that provide complementary insights without redundancy. Categories and each category's indicators are:

Moving Averages:
- close_50_sma: 50 SMA: A medium-term trend indicator. Usage: Identify trend direction and serve as dynamic support/resistance. Tips: It lags price; combine with faster indicators for timely signals.
- close_200_sma: 200 SMA: A long-term trend benchmark. Usage: Confirm overall market trend and identify golden/death cross setups. Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries.
- close_10_ema: 10 EMA: A responsive short-term average. Usage: Capture quick shifts in momentum and potential entry points. Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals.

MACD Related:
- macd: MACD: Computes momentum via differences of EMAs. Usage: Look for crossovers and divergence as signals of trend changes. Tips: Confirm with other indicators in low-volatility or sideways markets.
- macds: MACD Signal: An EMA smoothing of the MACD line. Usage: Use crossovers with the MACD line to trigger trades. Tips: Should be part of a broader strategy to avoid false positives.
- macdh: MACD Histogram: Shows the gap between the MACD line and its signal. Usage: Visualize momentum strength and spot divergence early. Tips: Can be volatile; complement with additional filters in fast-moving markets.

Momentum Indicators:
- rsi: RSI: Measures momentum to flag overbought/oversold conditions. Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis.

Volatility Indicators:
- boll: Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. Usage: Acts as a dynamic benchmark for price movement. Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals.
- boll_ub: Bollinger Upper Band: Typically 2 standard deviations above the middle line. Usage: Signals potential overbought conditions and breakout zones. Tips: Confirm signals with other tools; prices may ride the band in strong trends.
- boll_lb: Bollinger Lower Band: Typically 2 standard deviations below the middle line. Usage: Indicates potential oversold conditions. Tips: Use additional analysis to avoid false reversal signals.
- atr: ATR: Averages true range to measure volatility. Usage: Set stop-loss levels and adjust position sizes based on current market volatility. Tips: It's a reactive measure, so use it as part of a broader risk management strategy.

Volume-Based Indicators:
- vwma: VWMA: A moving average weighted by volume. Usage: Confirm trends by integrating price action with volume data. Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses.

- Select indicators that provide diverse and complementary information. Avoid redundancy (e.g., do not select both rsi and stochrsi). Also briefly explain why they are suitable for the given market context. When you tool call, please use the exact name of the indicators provided above as they are defined parameters, otherwise your call will fail. Please make sure to call get_stock_data first to retrieve the CSV that is needed to generate indicators. Then use get_indicators with the specific indicator names.

Before writing the final report, call get_verified_market_snapshot for this ticker and the current date, and treat it as the source of truth for any exact OHLCV, price-level, or indicator-value claim. If another tool's output conflicts with the verified snapshot, flag the discrepancy rather than inventing a reconciled number. Do not claim historical validation, support/resistance bounces, or exact percentage moves unless they are directly supported by tool output with concrete dates and prices. The snapshot ends with a Computed facts section holding the percentage distances, drawdown, 50-day versus 200-day crossover state, and volume ratio; quote those instead of calculating your own, and state the 50-day versus 200-day relationship exactly as given. If a tool says today's session was still open and its partial bar was left out, say that your figures are as of the previous finished session and do not call any price a close for the open day.

Write a very detailed and nuanced report of the trends you observe. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."""
            + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + guidance(TOOLS, _GUIDANCE)
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
                    " Report what your tools support; another agent decides the trade."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in TOOLS]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        result, report = take_turn(prompt, llm, TOOLS, state["messages"])

        return {
            "messages": [result],
            "market_report": report,
        }

    return market_analyst_node
