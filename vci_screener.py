#!/usr/bin/env python3
"""
vci_screener.py — VCI Section 2 systematic screener
Replaces Finviz for theme-based near-52wk-low discovery.
Uses pre-built curated ticker universe per theme. Surfaces the FULL cap-band universe
tagged by 52wk position — it no longer gates out already-inflecting bottleneck names
(the old "<=30% of 52wk low" hard filter rewarded falling knives and hid names whose
thesis had started to be recognised). Filters retained:
  - Market cap $300M - $50B
  - Sufficient liquidity (price > $0.50 / £0.10)
Each result is tagged NEAR-LOW (<=30%), MID-RANGE (30-70%) or ELEVATED (>70%).
Use --near-low-only to restore the legacy <=30% behaviour; --max-pos N for a custom cap.

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
#                6=Real-time financial infrastructure,
#                9=Optical/photonic infrastructure, 10=Quantum computing
# (Themes 7=Space economy and 8=Critical minerals have no curated screener universe yet
#  — they fall through to the Finviz/web fallback in Section 2.1A Pass 2.)
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
    },
    9: {
        'name': 'Optical / Photonic Infrastructure',
        'description': 'CPO, optical circuit switch, silicon photonics, 800G/1.6T transceivers — the optical nervous system of AI compute. Section 2 priority: sub-$5B component/substrate layer (the large-cap optical names are partly priced).',
        'tickers': [
            # Sub-$5B component / pre-inflection bottleneck layer (PRIORITY)
            'LWLG',   # Lightwave Logic — polymer electro-optic modulator platform
            'POET',   # POET Technologies — optical interposer (electronics+photonics integration)
            'AAOI',   # Applied Optoelectronics — datacenter optical transceivers
            'MTSI',   # MACOM Technology — analog/RF + photonics for optical
            'LASR',   # nLight — high-power semiconductor/fiber lasers
            'AXTI',   # AXT Inc — InP/GaAs compound-semi substrates for optical (also Theme 1)
            'VIAV',   # Viavi Solutions — optical test & measurement, 3D sensing
            'FN',     # Fabrinet — precision optical manufacturing/packaging (assembles LITE/COHR)
            # Larger optical names — cap filter ($50B) will drop any that have outgrown the band
            'LITE',   # Lumentum — CPO/optical circuit switch leader (NVIDIA-backed)
            'COHR',   # Coherent — vertically integrated optical/photonics (NVIDIA-backed)
            'CIEN',   # Ciena — coherent optical networking / AI fibre backbone
            'GLW',    # Corning — optical connectivity (NVIDIA partnership) [likely >$50B -> filtered]
            # NOTE private bottleneck names to track (not investable): Ayar Labs, Lightmatter, Celestial AI.
        ]
    },
    10: {
        'name': 'Quantum Computing Infrastructure',
        'description': 'Trapped-ion / superconducting / neutral-atom / photonic quantum hardware + post-quantum security silicon. Pre-inflection (3-5yr to commercial); R&D >50% of revenue is the defining feature, not a disqualifier.',
        'tickers': [
            'IONQ',   # IonQ — trapped-ion, most mature pure-play (~$19B)
            'RGTI',   # Rigetti — superconducting gate-model
            'QBTS',   # D-Wave Quantum — annealing + gate-model
            'QUBT',   # Quantum Computing Inc — photonic / integrated-photonic chips + foundry
            'INFQ',   # Infleqtion — neutral-atom quantum compute + sensing (NYSE from Feb 2026)
            'ARQQ',   # Arqit Quantum — quantum-safe encryption / PQC
            'LAES',   # SEALSQ — post-quantum cryptography secure semiconductors
            # EXCLUDED by design: QMCO (Quantum Corp = data storage, name confusion),
            #                     QSI (Quantum-Si = protein sequencing -> Theme 4, not compute).
        ]
    }
}

UK_SUFFIXES = ('.L', '.l')
MAX_MKTCAP = 50e9
MIN_MKTCAP = 300e6
NEAR_LOW_MAX = 30.0   # <=30% of 52wk range => NEAR-LOW tag (legacy filter boundary)
MID_MAX = 70.0        # 30-70% => MID-RANGE; >70% => ELEVATED
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


def position_tag(pos):
    if pos <= NEAR_LOW_MAX:
        return 'NEAR-LOW'
    if pos <= MID_MAX:
        return 'MID-RANGE'
    return 'ELEVATED'


def screen_ticker(sym, max_pos=None):
    """Return dict of metrics if passes cap/liquidity filters, else None.
    max_pos: optional upper 52wk-position cap (legacy behaviour when set to 30)."""
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
        if max_pos is not None and pos52 > max_pos:
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
            'position_tag': position_tag(pos52),
            'mktcap': mktcap,
        }
    except Exception:
        return None


def fmt_cap(v):
    if v >= 1e9:
        return f'{v/1e9:.1f}B'
    return f'{v/1e6:.0f}M'


def run_themes(theme_ids, max_pos=None):
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
        _filt = f'52wk pos <={max_pos:.0f}% (legacy)' if max_pos is not None else 'all positions tagged'
        print(f'  Universe: {len(tickers)} tickers | Filter: cap $300M-$50B, {_filt}')
        print(f'  Fetching data', end='', flush=True)

        for sym in tickers:
            result = screen_ticker(sym, max_pos=max_pos)
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

    # Sort by theme, then by position (NEAR-LOW first within a theme).
    results.sort(key=lambda r: (r['theme_id'], r['pos52']))

    n_low = sum(1 for r in results if r['position_tag'] == 'NEAR-LOW')
    n_mid = sum(1 for r in results if r['position_tag'] == 'MID-RANGE')
    n_elev = sum(1 for r in results if r['position_tag'] == 'ELEVATED')

    print(f'\n{"="*92}')
    print(f'  SCREENER RESULTS - {len(results)} tickers in cap band $300M-$50B '
          f'(NEAR-LOW {n_low} | MID {n_mid} | ELEVATED {n_elev})')
    print(f'  ELEVATED names are NOT discarded: an inflecting bottleneck name is a re-score trigger, not a reject.')
    print(f'{"="*92}')
    print(f'  {"Ticker":<8} {"Company":<36} {"Exch":<6} {"Price":>8} {"Pos%":>6} {"Position":<10} {"Cap":>7}  Theme')
    print(f'  {"-"*7} {"-"*35} {"-"*5} {"-"*8} {"-"*6} {"-"*9} {"-"*7}  -----')

    for r in results:
        p = r['prefix']
        print(
            f'  {r["ticker"]:<8} {r["name"]:<36} {r["exchange"]:<6} '
            f'{p}{r["price"]:>7.2f} {r["pos52"]:>5.1f}% {r["position_tag"]:<10} '
            f'{fmt_cap(r["mktcap"]):>7}  T{r["theme_id"]}'
        )

    print(f'\n  Next step: run vci_batch1_pull.py on these tickers for Part A data.')
    print(f'  Example: python vci_batch1_pull.py', ' '.join(r['ticker'] for r in results[:8]))


def main():
    raw = sys.argv[1:]
    # Extract flags (theme numbers remain positional)
    max_pos = None
    if '--near-low-only' in raw:
        max_pos = NEAR_LOW_MAX
        raw = [a for a in raw if a != '--near-low-only']
    if '--max-pos' in raw:
        i = raw.index('--max-pos')
        try:
            max_pos = float(raw[i + 1])
            del raw[i:i + 2]
        except (IndexError, ValueError):
            print('[WARN] --max-pos requires a number; ignoring.')
            del raw[i:i + 1]
    args = raw
    if not args:
        print('Usage: python vci_screener.py [theme_number|all] [theme2] ...')
        print('       python vci_screener.py 1')
        print('       python vci_screener.py 1 4')
        print('       python vci_screener.py all')
        print()
        print('Themes: 1=AI Compute Substrate  2=AI-Native Workflow  3=Energy Infra for AI')
        print('        4=Biological Computation  5=Spatial Computing  6=Financial Infrastructure')
        print('        9=Optical/Photonic Infrastructure  10=Quantum Computing')
        print('Flags:  --near-low-only (legacy <=30%)   --max-pos N (custom 52wk-position cap)')
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

    results, themes_run = run_themes(theme_ids, max_pos=max_pos)
    print_results(results)


if __name__ == '__main__':
    main()
