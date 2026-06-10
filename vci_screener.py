#!/usr/bin/env python3
"""
vci_screener.py — VCI Section 2 systematic screener
Replaces Finviz for theme-based near-52wk-low discovery.
Uses pre-built curated ticker universe per theme, filters to:
  - 52wk position <= 30%  (stock within 30% of 52wk low)
  - Market cap $300M – $50B
  - Sufficient liquidity (price > $0.50 / £0.10)

Usage:
    python vci_screener.py 1           (Theme 1: AI compute substrate)
    python vci_screener.py 4           (Theme 4: Biological computation)
    python vci_screener.py all         (all themes, ranked by 52wk position)
    python vci_screener.py 1 2         (Themes 1 and 2)

UNIVERSE MAINTENANCE: Review and update ticker lists quarterly (each March, June, Sep, Dec run).
Remove any acquired/delisted tickers. Add newly listed candidates with market cap $300M–$50B.
Last updated: June 2026.
"""

import sys
try:
    import isa_env_guard  # noqa  (disk guardrail: forces temp + yfinance cache onto tmpfs /dev/shm)
except Exception:
    pass
import math
import time
import yfinance as yf

# --------------------------------------------------------------------------
# CURATED UNIVERSE — update quarterly
# Theme mapping: 1=AI compute substrate, 2=AI-native workflow,
#                3=Energy infra for AI, 4=Biological computation,
#                5=Spatial computing / physical-digital,
#                6=Real-time financial infrastructure
# --------------------------------------------------------------------------

UNIVERSE = {
    1: {
        'name': 'AI Infrastructure Compute Substrate',
        'description': 'Semiconductor, networking, packaging, power — beyond hyperscaler cloud',
        'tickers': [
            # Semiconductors / networking / silicon
            'ALAB',   # Astera Labs — PCIe/CXL connectivity fabric
            'CRDO',   # Credo Technology — high-speed SerDes
            'AMBA',   # Ambarella — edge AI inference silicon
            'CEVA',   # CEVA — semiconductor IP licensing, AI DSP
            'MTSI',   # MACOM Technology — analog/RF semiconductors
            'ALGM',   # Allegro MicroSystems — power/sensing
            'AXTI',   # AXT Inc — compound semiconductor substrates
            'COHU',   # Cohu — semiconductor test handling
            'ACMR',   # ACM Research — semiconductor cleaning
            'KLIC',   # Kulicke & Soffa — wire bonding / advanced packaging
            'ONTO',   # Onto Innovation — process control / metrology
            'ICHR',   # Ichor Holdings — semiconductor gas delivery
            'FORM',   # FormFactor — wafer probe
            'BRKS',   # Brooks Automation — semiconductor automation
            'MKSI',   # MKS Instruments — process control
            'CAMT',   # Camtek — inspection / metrology
            'UCTT',   # Ultra Clean Holdings — semiconductor parts
            'AEHR',   # Aehr Test Systems — wafer-level burn-in
            'POWI',   # Power Integrations — power conversion
            'SLAB',   # Silicon Labs — IoT/wireless silicon
            'QUIK',   # QuickLogic — embedded FPGA / edge AI
            'SMCI',   # Super Micro Computer — AI server infrastructure
            'PDYN',   # Palladyne AI — edge AI
            'NXPI',   # NXP Semiconductors (large, may be filtered)
        ]
    },
    2: {
        'name': 'AI-Native Enterprise Workflow',
        'description': 'Workflow OS where AI is structurally embedded — not LLM wrappers',
        'tickers': [
            'PATH',   # UiPath — RPA/automation platform
            'AI',     # C3.ai — enterprise AI applications
            'CWAN',   # Clearwater Analytics — investment accounting SaaS
            'SPSC',   # SPS Commerce — supply chain EDI platform
            'TASK',   # TaskUs — digital CX / AI-enabled operations
            'RAMP',   # LiveRamp — data connectivity platform
            'BRZE',   # Braze — customer engagement platform
            'GTLB',   # GitLab — DevSecOps platform
            'CFLT',   # Confluent — data streaming platform
            'ASAN',   # Asana — work management
            'TOST',   # Toast — restaurant management OS
            'SPRK',   # Sprinklr — unified customer experience
            'DOMO',   # Domo — business intelligence
            'FROG',   # JFrog — software supply chain
            'APPN',   # Appian — low-code automation
            'SUMO',   # Sumo Logic — cloud SIEM (if still listed)
            'VNET',   # 21Vianet — note: China exposure, check VIE
            'HUBS',   # HubSpot (large-cap, likely filtered)
            'MNDY',   # Monday.com — work OS
            'SMAR',   # Smartsheet — work management
        ]
    },
    3: {
        'name': 'Energy Infrastructure for AI',
        'description': 'Power density, cooling, grid interconnect, SMR — physical AI data centre supply chain',
        'tickers': [
            'AMPL',   # Amplitude (wrong sector — placeholder, remove if confirmed)
            'SHLS',   # Shoals Technologies — solar balance of system
            'FLUX',   # Flux Power — lithium batteries for forklifts/industrial
            'STEM',   # Stem Inc — energy storage AI optimisation
            'NOVA',   # Sunnova Energy — solar/storage
            'BE',     # Bloom Energy — solid oxide fuel cells
            'NRDY',   # Nerdy Inc — education (wrong sector, placeholder)
            'SMR',    # NuScale Power — small modular reactor
            'NNE',    # Nano Nuclear Energy — micro reactor
            'OKLO',   # Oklo — advanced fission reactor
            'ARRY',   # Array Technologies — solar trackers
            'ENPH',   # Enphase Energy — microinverters
            'SEDG',   # SolarEdge — inverters
            'RUN',    # Sunrun — residential solar
            'SPWR',   # SunPower — solar
            'NEP',    # NextEra Energy Partners — clean energy
            'CWEN',   # Clearway Energy
            'AMRC',   # Ameresco — energy efficiency
            'CDPYF',  # Candu Energy (TSX proxy)
            'BWXT',   # BWX Technologies — nuclear components
        ]
    },
    4: {
        'name': 'Biological Computation',
        'description': 'AI applied at biological substrate — sequencing, drug design, synthetic biology',
        'tickers': [
            'RXRX',   # Recursion Pharmaceuticals — AI drug discovery OS
            'ABCL',   # AbCellera Biologics — antibody discovery platform
            'ONT.L',  # Oxford Nanopore Technologies — long-read sequencing (LSE)
            'PACB',   # Pacific Biosciences — long-read sequencing
            'SEER',   # Seer Bio — proteomics
            'TWST',   # Twist Bioscience — synthetic DNA
            'CODE',   # Codex DNA (acquired — remove if confirmed)
            'BEAM',   # Beam Therapeutics — base editing
            'EDIT',   # Editas Medicine — CRISPR
            'NTLA',   # Intellia Therapeutics — CRISPR
            'CRSP',   # CRISPR Therapeutics
            'SDGR',   # Schrödinger — molecular simulation platform (tool, not OS)
            'GFAI',   # Guardforce AI (different sector, remove)
            'FATE',   # Fate Therapeutics — cell therapy
            'IMVT',   # Immunovant — FcRn antibody
            'AGEN',   # Agenus — cancer immunology
            'ALNY',   # Alnylam — RNAi therapeutics
            'NTRA',   # Natera — liquid biopsy / genomics
            'EXAS',   # Exact Sciences — cancer screening
            'INVA',   # Innoviva — specialty pharma
            'RVMD',   # Revolution Medicines — RAS oncology
            'ARKG',   # ARK Genomic Revolution ETF (screener proxy for biotech universe)
        ]
    },
    5: {
        'name': 'Spatial Computing and Physical-Digital Interface',
        'description': 'Industrial AR/VR, autonomous perception, digital twin platforms',
        'tickers': [
            'MVIS',   # MicroVision — MEMS LIDAR
            'OUST',   # Ouster — digital LIDAR
            'AEVA',   # Aeva Technologies — FMCW LIDAR
            'INVZ',   # Innoviz Technologies — solid-state LIDAR
            'AEYE',   # AudioEye (wrong sector, remove)
            'LIDR',   # AEye — LIDAR
            'DM',     # Desktop Metal — 3D printing
            'MTTR',   # Matterport — 3D digital twin platform
            'PTC',    # PTC Inc — industrial IoT/AR platform
            'ANSYS',  # Ansys — simulation software (large-cap, filter)
            'PRLB',   # Proto Labs — digital manufacturing (if listed)
            'SHPW',   # Shapeways — digital manufacturing
            'KSCP',   # Knightscope — autonomous security robots
            'RNG',    # RingCentral (different sector, remove)
            'VLDR',   # Velodyne (merged into Ouster — remove)
            'XPEV',   # Xpeng (China VIE — excluded structurally)
            'SWKS',   # Skyworks — note: connectivity silicon
            'LNKN',   # placeholder — remove
            'ISRG',   # Intuitive Surgical — robotic surgical platform (large-cap)
            'NNDM',   # Nano Dimension — 3D printing
        ]
    },
    6: {
        'name': 'Real-Time Financial Infrastructure',
        'description': 'Embedded payments, instant settlement, programmable money rails',
        'tickers': [
            'PAYO',   # Payoneer — cross-border payments
            'FLYW',   # Flywire — vertical payments
            'ALKT',   # Alkami Technology — digital banking platform
            'I2GO',   # placeholder
            'XP',     # XP Inc — Brazilian financial platform
            'NU',     # Nu Holdings — digital banking LatAm
            'SOFI',   # SoFi Technologies — digital financial services
            'UPST',   # Upstart — AI lending
            'AFRM',   # Affirm — BNPL/fintech
            'HOOD',   # Robinhood — retail fintech
            'LMND',   # Lemonade — AI insurance
            'ROOT',   # Root Insurance — AI auto insurance
            'WEX',    # WEX Inc — fleet/corporate payments
            'REPX',   # Riley Exploration (wrong sector, remove)
            'BILL',   # Bill Holdings — SMB financial operations
            'PAYC',   # Paycom — HR/payroll SaaS (large-cap)
            'PLTK',   # Playtika (wrong sector)
            'MQ',     # Marqeta — card issuing platform
            'RPAY',   # Repay Holdings — integrated payments
            'PRTH',   # Priority Technology — embedded payments
        ]
    }
}

UK_SUFFIXES = ('.L', '.l')
MAX_MKTCAP = 50e9
MIN_MKTCAP = 300e6
MAX_52WK_POS = 30.0
MIN_PRICE = 0.10


def is_uk(sym):
    return any(sym.endswith(s) for s in UK_SUFFIXES)


def gbp_fix(val, sym):
    if is_uk(sym) and isinstance(val, (int, float)) and not math.isnan(val) and val > 500:
        return val / 100.0
    return val


def safe(val):
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def screen_ticker(sym):
    """Return dict of metrics if passes filters, else None."""
    try:
        t = yf.Ticker(sym)
        info = t.info
        price_raw = safe(info.get('currentPrice') or info.get('regularMarketPrice'))
        hi52_raw = safe(info.get('fiftyTwoWeekHigh'))
        lo52_raw = safe(info.get('fiftyTwoWeekLow'))
        mktcap = safe(info.get('marketCap'))
        name = info.get('shortName') or info.get('longName') or sym
        exchange = info.get('exchange') or 'N/A'

        price = gbp_fix(price_raw, sym)
        hi52 = gbp_fix(hi52_raw, sym)
        lo52 = gbp_fix(lo52_raw, sym)

        if not all([price, hi52, lo52, mktcap]):
            return None
        if price < MIN_PRICE:
            return None
        if mktcap < MIN_MKTCAP or mktcap > MAX_MKTCAP:
            return None
        if hi52 == lo52:
            return None

        pos52 = (price - lo52) / (hi52 - lo52) * 100
        if pos52 > MAX_52WK_POS:
            return None

        p = '£' if is_uk(sym) else '$'
        return {
            'ticker': sym,
            'name': name[:35],
            'exchange': exchange,
            'price': price,
            'prefix': p,
            'hi52': hi52,
            'lo52': lo52,
            'pos52': pos52,
            'mktcap': mktcap,
        }
    except Exception:
        return None


def fmt_cap(v):
    if v >= 1e9:
        return f'{v/1e9:.1f}B'
    return f'{v/1e6:.0f}M'


def run_themes(theme_ids):
    results = []
    themes_run = []

    for tid in theme_ids:
        if tid not in UNIVERSE:
            print(f'  [SKIP] Theme {tid} not defined. Valid themes: 1-6 or "all".')
            continue
        theme = UNIVERSE[tid]
        themes_run.append(theme['name'])
        tickers = theme['tickers']
        print(f'\nScreening Theme {tid}: {theme["name"]}')
        print(f'  {theme["description"]}')
        print(f'  Universe: {len(tickers)} tickers | Filter: 52wk pos <=30%, cap $300M–$50B')
        print(f'  Fetching data', end='', flush=True)

        for sym in tickers:
            result = screen_ticker(sym)
            time.sleep(0.15)  # rate limiting
            print('.', end='', flush=True)
            if result:
                result['theme_id'] = tid
                result['theme_name'] = theme['name']
                results.append(result)
        print(' done.')

    return results, themes_run


def print_results(results):
    if not results:
        print('\n  No tickers passed all filters.')
        print('  Consider: (a) Finviz.com fallback, or (b) web search "[theme] stocks near 52-week low small-cap [year]"')
        return

    results.sort(key=lambda r: r['pos52'])

    print(f'\n{"="*80}')
    print(f'  SCREENER RESULTS — {len(results)} tickers within 30% of 52wk low, cap $300M–$50B')
    print(f'{"="*80}')
    print(f'  {"Ticker":<8} {"Company":<36} {"Exch":<6} {"Price":>8} {"52wk Lo":>8} {"52wk Hi":>8} {"Pos%":>6} {"Cap":>7}  Theme')
    print(f'  {"-"*7} {"-"*35} {"-"*5} {"-"*8} {"-"*8} {"-"*8} {"-"*6} {"-"*7}  -----')

    for r in results:
        p = r['prefix']
        print(
            f'  {r["ticker"]:<8} {r["name"]:<36} {r["exchange"]:<6} '
            f'{p}{r["price"]:>7.2f} {p}{r["lo52"]:>7.2f} {p}{r["hi52"]:>7.2f} '
            f'{r["pos52"]:>5.1f}% {fmt_cap(r["mktcap"]):>7}  T{r["theme_id"]}'
        )

    print(f'\n  Next step: run vci_batch1_pull.py on these tickers for Part A data.')
    print(f'  Example: python vci_batch1_pull.py', ' '.join(r['ticker'] for r in results[:8]))


def main():
    args = sys.argv[1:]
    if not args:
        print('Usage: python vci_screener.py [theme_number|all] [theme2] ...')
        print('       python vci_screener.py 1')
        print('       python vci_screener.py 1 4')
        print('       python vci_screener.py all')
        print()
        print('Themes: 1=AI Compute Substrate  2=AI-Native Workflow  3=Energy Infra for AI')
        print('        4=Biological Computation  5=Spatial Computing  6=Financial Infrastructure')
        sys.exit(1)

    if args[0].lower() == 'all':
        theme_ids = list(UNIVERSE.keys())
    else:
        theme_ids = []
        for a in args:
            try:
                theme_ids.append(int(a))
            except ValueError:
                print(f'[WARN] Ignoring argument "{a}" — expected integer theme number or "all".')

    if not theme_ids:
        print('No valid theme IDs provided.')
        sys.exit(1)

    results, themes_run = run_themes(theme_ids)
    print_results(results)


if __name__ == '__main__':
    main()
