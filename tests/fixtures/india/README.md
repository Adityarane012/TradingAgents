# India data fixtures

Real responses captured from NSE's public JSON API on 2026-09-20, trimmed
(fewer rows/strikes) but otherwise unedited, so parser tests exercise the
shapes NSE actually returns. Tests never touch the network.

| File | Endpoint |
|---|---|
| `nse_fiidii.json` | `fiidiiTradeReact` |
| `nse_allindices.json` | `allIndices` (Nifty 50, India VIX, Nifty Bank rows) |
| `nse_option_contract_info.json` | `option-chain-contract-info?symbol=NIFTY` |
| `nse_option_chain_v3.json` | `option-chain-v3` (14 strikes nearest the underlying) |
| `nse_shareholding_reliance.json` | `corporate-share-holdings-master?symbol=RELIANCE` (8 rows) |
| `nse_corp_actions_reliance.json` | `corporates-corporateActions?symbol=RELIANCE` (5 rows) |
| `nse_announcements_reliance.json` | `corporate-announcements?symbol=RELIANCE&from_date&to_date` (6 rows) |

If NSE changes a response shape, recapture the affected file and update the
parser and tests together.
