#!/usr/bin/env python3
"""
Exolix Crypto Swap CLI
A rich terminal interface for performing crypto swaps via the Exolix API.

Requirements:
    pip install requests rich qrcode argparse
"""

import argparse
import sys
import time
import os
import requests
import qrcode
from io import StringIO
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align
from rich.columns import Columns
from rich.rule import Rule
from rich import box
from rich.style import Style
from rich.padding import Padding
from datetime import datetime

# ── Configuration ────────────────────────────────────────────────────────────
API_BASE = "https://exolix.com/api/v2"
POLL_INTERVAL = 15  # seconds

STATUS_STYLES = {
    "wait":         ("yellow",       "⏳ Waiting for deposit"),
    "confirmation": ("cyan",         "🔄 Confirming deposit"),
    "confirmed":    ("blue",         "✅ Deposit confirmed"),
    "exchanging":   ("magenta",      "🔀 Exchanging"),
    "sending":      ("bright_blue",  "📤 Sending funds"),
    "success":      ("bright_green", "🎉 Success!"),
    "overdue":      ("red",          "⌛ Overdue"),
    "refund":       ("orange1",      "↩  Refund in progress"),
    "refunded":     ("bright_red",   "↩  Refunded"),
}

console = Console()

# ── API helpers ───────────────────────────────────────────────────────────────

def get_headers(api_key: str) -> dict:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": api_key,
    }

def get_rate(api_key: str, coin_from: str, coin_to: str,
             network_from: str, network_to: str,
             amount: float, rate_type: str) -> dict:
    """Fetch the current exchange rate estimate."""
    params = {
        "coinFrom":    coin_from,
        "coinTo":      coin_to,
        "networkFrom": network_from,
        "networkTo":   network_to,
        "amount":      amount,
        "rateType":    rate_type,
    }
    resp = requests.get(f"{API_BASE}/rate",
                        headers=get_headers(api_key),
                        params={k: v for k, v in params.items() if v})
    resp.raise_for_status()
    return resp.json()

def create_transaction(api_key: str, payload: dict) -> dict:
    """Create a new exchange transaction."""
    resp = requests.post(f"{API_BASE}/transactions",
                         headers=get_headers(api_key),
                         json=payload)
    resp.raise_for_status()
    return resp.json()

def get_transaction(api_key: str, tx_id: str) -> dict:
    """Poll a transaction for its latest state."""
    resp = requests.get(f"{API_BASE}/transactions/{tx_id}",
                        headers=get_headers(api_key))
    resp.raise_for_status()
    return resp.json()

# ── QR code helpers ───────────────────────────────────────────────────────────

def build_qr_string(address: str, extra_id: str | None,
                    coin: str, amount: float) -> str:
    """
    Build a URI suitable for most crypto wallet QR scanners.
    Falls back to plain address when no standard URI scheme is known.
    """
    schemes = {
        "BTC": "bitcoin", "ETH": "ethereum", "LTC": "litecoin",
        "BCH": "bitcoincash", "XMR": "monero", "XRP": "ripple",
        "DOGE": "dogecoin", "SOL": "solana", "ADA": "cardano",
    }
    scheme = schemes.get(coin.upper())
    if scheme:
        uri = f"{scheme}:{address}?amount={amount}"
        if extra_id:
            uri += f"&dt={extra_id}"
        return uri
    return address

def qr_to_rich_text(data: str) -> Text:
    """
    Render a QR code using half-block ▄ characters.
    Each cell = 1 module wide × 2 modules tall.
      foreground (▄ lower half) = bottom module colour
      background (▄ upper half) = top module colour
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    # Ensure even row count
    if len(matrix) % 2 != 0:
        matrix.append([False] * len(matrix[0]))

    result = Text(no_wrap=True, justify="left")

    for row_idx in range(0, len(matrix), 2):
        top_row = matrix[row_idx]
        bot_row = matrix[row_idx + 1]
        for top, bot in zip(top_row, bot_row):
            bg = "black" if top else "white"
            fg = "black" if bot else "white"
            result.append("▄", style=f"{fg} on {bg}")
        result.append("\n")

    return result


# ── Rich UI builders ──────────────────────────────────────────────────────────

def make_status_badge(status: str) -> Text:
    color, label = STATUS_STYLES.get(status, ("white", status))
    return Text(f" {label} ", style=f"bold {color} on grey15")

def make_info_table(tx: dict) -> Table:
    """Build the transaction-info table."""
    cf = tx.get("coinFrom", {})
    ct = tx.get("coinTo", {})

    tbl = Table(
        show_header=False,
        box=box.SIMPLE,
        padding=(0, 1),
        expand=True,
    )
    tbl.add_column("Field",  style="bold cyan",  ratio=1)
    tbl.add_column("Value",  style="white",       ratio=3)

    rows = [
        ("Transaction ID",  tx.get("id", "—")),
        ("Rate Type",       tx.get("rateType", "—").upper()),
        ("Rate",            f"1 {cf.get('coinCode','?')} ≈ "
                            f"{tx.get('rate', '—')} {ct.get('coinCode','?')}"),
        ("Created",         tx.get("createdAt", "—")),
    ]
    for k, v in rows:
        tbl.add_row(k, str(v))

    return tbl

def make_exchange_table(tx: dict) -> Table:
    """Build the from/to exchange amounts table."""
    cf = tx.get("coinFrom", {})
    ct = tx.get("coinTo", {})

    tbl = Table(
        title="Exchange",
        title_style="bold white",
        box=box.ROUNDED,
        padding=(0, 2),
        expand=True,
    )
    tbl.add_column("",        style="bold cyan", justify="right")
    tbl.add_column("Coin",    style="bold yellow")
    tbl.add_column("Network", style="dim white")
    tbl.add_column("Amount",  style="bold white", justify="right")

    tbl.add_row(
        "FROM",
        cf.get("coinCode", "?"),
        cf.get("networkShortName") or cf.get("networkName", "?"),
        f"{tx.get('amount', '—')}",
    )
    tbl.add_row(
        "TO",
        ct.get("coinCode", "?"),
        ct.get("networkShortName") or ct.get("networkName", "?"),
        f"≈ {tx.get('amountTo', '—')}",
    )
    return tbl

def make_address_table(tx: dict) -> Table:
    """Build deposit / withdrawal address table."""
    tbl = Table(
        show_header=False,
        box=box.SIMPLE,
        padding=(0, 1),
        expand=True,
    )
    tbl.add_column("Label", style="bold cyan",  ratio=1)
    tbl.add_column("Value", style="bold white", ratio=4, overflow="fold")

    deposit_addr  = tx.get("depositAddress", "—")
    deposit_extra = tx.get("depositExtraId")
    withdraw_addr = tx.get("withdrawalAddress", "—")
    withdraw_extra = tx.get("withdrawalExtraId")

    tbl.add_row("Deposit Address",  deposit_addr)
    if deposit_extra:
        tbl.add_row("  Deposit Extra ID", deposit_extra)
    tbl.add_row("Withdrawal Address", withdraw_addr)
    if withdraw_extra:
        tbl.add_row("  Withdrawal Extra ID", withdraw_extra)

    refund = tx.get("refundAddress")
    if refund:
        tbl.add_row("Refund Address", refund)

    return tbl

def make_hash_table(tx: dict) -> Table:
    """Build inbound / outbound hash table."""
    h_in  = tx.get("hashIn",  {}) or {}
    h_out = tx.get("hashOut", {}) or {}

    tbl = Table(
        show_header=False,
        box=box.SIMPLE,
        padding=(0, 1),
        expand=True,
    )
    tbl.add_column("Label", style="bold cyan",  ratio=1)
    tbl.add_column("Value", style="dim white",  ratio=4, overflow="fold")

    tbl.add_row("Tx Hash In",
                h_in.get("hash") or "—")
    if h_in.get("link"):
        tbl.add_row("  Explorer In", h_in["link"])

    tbl.add_row("Tx Hash Out",
                h_out.get("hash") or "—")
    if h_out.get("link"):
        tbl.add_row("  Explorer Out", h_out["link"])

    return tbl

def make_qr_renderable(tx: dict):
    """
    Returns a single self-contained renderable for the QR panel body.
    Keeping it as a simple Group avoids the double-render bug caused by
    Text.assemble copying references that Layout then renders twice.
    """
    from rich.console import Group as RichGroup

    cf            = tx.get("coinFrom", {})
    deposit_addr  = tx.get("depositAddress", "")
    deposit_extra = tx.get("depositExtraId")
    amount        = tx.get("amount", 0)
    coin          = cf.get("coinCode", "")

    qr_data = build_qr_string(deposit_addr, deposit_extra, coin, amount)
    qr_text = qr_to_rich_text(qr_data)

    # Shorten address for display only
    if len(deposit_addr) > 28:
        addr_display = deposit_addr[:14] + "…" + deposit_addr[-10:]
    else:
        addr_display = deposit_addr

    addr_line = Text(addr_display, style="dim cyan", justify="center",
                     no_wrap=True)

    renderables = [Align.center(qr_text), Align.center(addr_line)]

    if deposit_extra:
        memo_line = Text(f"Memo: {deposit_extra}", style="dim yellow",
                         justify="center", no_wrap=True)
        renderables.append(Align.center(memo_line))

    return RichGroup(*renderables)

def make_qr_panel(tx: dict) -> Panel:
    return Panel(
        make_qr_renderable(tx),
        title="[bold cyan]📷  Scan to Deposit[/bold cyan]",
        border_style="cyan",
        padding=(0, 1),
    )


def build_layout(tx: dict, last_polled: str, poll_count: int) -> Layout:
    status       = tx.get("status", "unknown")
    color, label = STATUS_STYLES.get(status, ("white", status))

    # ── Header ────────────────────────────────────────────────────────────────
    header_text = Text.assemble(
        ("  ⚡ EXOLIX SWAP  ", "bold white on #1a1a2e"),
        ("  ", ""),
        make_status_badge(status),
        (f"   polled {poll_count}× · last update: {last_polled}  ",
         "dim white"),
    )
    header = Panel(header_text, style=f"bold {color}", box=box.HEAVY)

    # ── Left column panels ────────────────────────────────────────────────────
    exchange_panel = Panel(
        make_exchange_table(tx),
        title="[bold yellow]Swap Details[/bold yellow]",
        border_style="yellow",
    )
    info_panel = Panel(
        make_info_table(tx),
        title="[bold cyan]Transaction Info[/bold cyan]",
        border_style="cyan",
    )
    addr_panel = Panel(
        make_address_table(tx),
        title="[bold green]Addresses[/bold green]",
        border_style="green",
    )
    hash_panel = Panel(
        make_hash_table(tx),
        title="[bold magenta]Transaction Hashes[/bold magenta]",
        border_style="magenta",
    )

    # ── Footer ────────────────────────────────────────────────────────────────
    if status == "success":
        footer_msg = Text("✅  Swap complete! Press Ctrl+C to exit.",
                          style="bold bright_green", justify="center")
    elif status in ("overdue", "refund", "refunded"):
        footer_msg = Text(f"⚠️  Transaction {status}. Press Ctrl+C to exit.",
                          style="bold red", justify="center")
    else:
        footer_msg = Text(
            f"⏱  Next refresh in {POLL_INTERVAL}s — Press Ctrl+C to abort.",
            style="dim white", justify="center")
    footer = Panel(footer_msg, style="dim", box=box.MINIMAL)

    # ── Layout tree ───────────────────────────────────────────────────────────
    layout = Layout()

    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body",   ratio=1),
        Layout(name="footer", size=3),
    )

    layout["header"].update(header)
    layout["footer"].update(footer)

    layout["body"].split_row(
        Layout(name="left", ratio=3),
        Layout(name="qr",   ratio=2),
    )

    # QR lives ONLY in the right column — updated once via .update()
    layout["body"]["qr"].update(
        Panel(
            make_qr_renderable(tx),
            title="[bold cyan]📷  Scan to Deposit[/bold cyan]",
            border_style="cyan",
            padding=(0, 1),
        )
    )

    # Left column split — use ratio not size so they scale with terminal height
    layout["body"]["left"].split_column(
        Layout(name="exchange",  ratio=2),
        Layout(name="info",      ratio=2),
        Layout(name="addresses", ratio=3),
        Layout(name="hashes",    ratio=2),
    )

    layout["body"]["left"]["exchange"].update(exchange_panel)
    layout["body"]["left"]["info"].update(info_panel)
    layout["body"]["left"]["addresses"].update(addr_panel)
    layout["body"]["left"]["hashes"].update(hash_panel)

    return layout

# ── Pre-flight confirmation prompt ────────────────────────────────────────────

def confirm_swap(rate_info: dict, args: argparse.Namespace) -> bool:
    """Show a confirmation prompt before creating the transaction."""
    console.print()
    console.print(Rule("[bold cyan]Swap Preview[/bold cyan]"))

    tbl = Table(box=box.ROUNDED, show_header=False, padding=(0, 2))
    tbl.add_column("", style="bold cyan")
    tbl.add_column("", style="white")

    tbl.add_row("You send",
                f"[bold yellow]{args.amount} {args.coin_from.upper()}[/]")
    tbl.add_row("You receive",
                f"[bold green]≈ {rate_info.get('toAmount', '?')} "
                f"{args.coin_to.upper()}[/]")
    tbl.add_row("Rate",
                f"1 {args.coin_from.upper()} ≈ "
                f"{rate_info.get('rate', '?')} {args.coin_to.upper()}")
    tbl.add_row("Min Amount",  str(rate_info.get("minAmount", "?")))
    tbl.add_row("Max Amount",  str(rate_info.get("maxAmount", "?")))
    tbl.add_row("Rate Type",   args.rate_type.upper())
    tbl.add_row("Withdrawal",  args.withdrawal_address)
    if args.withdrawal_extra_id:
        tbl.add_row("Withdrawal Extra ID", args.withdrawal_extra_id)
    if args.refund_address:
        tbl.add_row("Refund Address", args.refund_address)

    console.print(Align.center(tbl))
    console.print()

    answer = console.input(
        "[bold white]Proceed with swap? [[bold green]y[/]/[bold red]n[/]] → [/]"
    ).strip().lower()
    return answer in ("y", "yes")

# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="exolix_swap",
        description="Perform a crypto swap via the Exolix API with a rich TUI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python exolix_swap.py --coin-from BTC --coin-to ETH --amount 0.01 \\
      --withdrawal-address 0xYourEthAddress

  python exolix_swap.py --coin-from ETH --coin-to XMR --amount 0.5 \\
      --network-from ETH --network-to XMR \\
      --withdrawal-address YourXMRAddress \\
      --refund-address 0xYourRefundAddress \\
      --rate-type fixed
        """,
    )

    # ── Core swap parameters ──────────────────────────────────────────────────
    parser.add_argument("--coin-from",            required=True,
                        help="Source coin code, e.g. BTC")
    parser.add_argument("--coin-to",              required=True,
                        help="Destination coin code, e.g. ETH")
    parser.add_argument("--amount",               required=True, type=float,
                        help="Amount of source coin to swap")
    parser.add_argument("--withdrawal-address",   required=True,
                        help="Destination address for the swapped coins")

    # ── Optional parameters ───────────────────────────────────────────────────
    parser.add_argument("--network-from",         default="",
                        help="Source network (defaults to coin default)")
    parser.add_argument("--network-to",           default="",
                        help="Destination network (defaults to coin default)")
    parser.add_argument("--withdrawal-extra-id",  default="",
                        help="Extra ID / memo for withdrawal address")
    parser.add_argument("--refund-address",       default="",
                        help="Address for refund if swap fails")
    parser.add_argument("--refund-extra-id",      default="",
                        help="Extra ID / memo for refund address")
    parser.add_argument("--rate-type",            default="float",
                        choices=["float", "fixed"],
                        help="float (default) or fixed rate")

    # ── Auth ──────────────────────────────────────────────────────────────────
    parser.add_argument("--api-key",
                        default=os.environ.get("EXOLIX_API_KEY", ""),
                        help="Exolix API key (or set EXOLIX_API_KEY env var)")

    return parser.parse_args()

def main() -> None:
    args = parse_args()

    # ── Validate API key ──────────────────────────────────────────────────────
    if not args.api_key:
        console.print(
            "[bold red]Error:[/] No API key provided.\n"
            "Pass [cyan]--api-key YOUR_KEY[/] or set the "
            "[cyan]EXOLIX_API_KEY[/] environment variable.",
        )
        sys.exit(1)

    coin_from = args.coin_from.upper()
    coin_to   = args.coin_to.upper()

    # ── Fetch rate ────────────────────────────────────────────────────────────
    console.print(
        f"\n[bold cyan]Fetching rate for "
        f"{args.amount} {coin_from} → {coin_to}…[/]"
    )
    try:
        rate_info = get_rate(
            api_key      = args.api_key,
            coin_from    = coin_from,
            coin_to      = coin_to,
            network_from = args.network_from,
            network_to   = args.network_to,
            amount       = args.amount,
            rate_type    = args.rate_type,
        )
    except requests.HTTPError as exc:
        console.print(f"[bold red]Rate fetch failed:[/] {exc}\n{exc.response.text}")
        sys.exit(1)

    # ── Check for error in rate response ─────────────────────────────────────
    if rate_info.get("message"):
        console.print(f"[bold red]API message:[/] {rate_info['message']}")
        sys.exit(1)

    # ── Show confirmation ─────────────────────────────────────────────────────
    if not confirm_swap(rate_info, args):
        console.print("[yellow]Swap cancelled.[/]")
        sys.exit(0)

    # ── Create transaction ────────────────────────────────────────────────────
    console.print("\n[bold cyan]Creating transaction…[/]")

    payload: dict = {
        "coinFrom":           coin_from,
        "coinTo":             coin_to,
        "networkFrom":        args.network_from or coin_from,
        "networkTo":          args.network_to   or coin_to,
        "amount":             args.amount,
        "withdrawalAddress":  args.withdrawal_address,
        "withdrawalExtraId":  args.withdrawal_extra_id,
        "rateType":           args.rate_type,
    }
    if args.refund_address:
        payload["refundAddress"]  = args.refund_address
        payload["refundExtraId"]  = args.refund_extra_id

    try:
        tx = create_transaction(args.api_key, payload)
    except requests.HTTPError as exc:
        console.print(f"[bold red]Transaction creation failed:[/] {exc}\n{exc.response.text}")
        sys.exit(1)

    tx_id = tx.get("id")
    if not tx_id:
        console.print("[bold red]No transaction ID returned — aborting.[/]")
        sys.exit(1)

    console.print(f"[bold green]Transaction created:[/] [cyan]{tx_id}[/]\n")

    # ── Poll loop ─────────────────────────────────────────────────────────────
    poll_count   = 1
    last_polled  = datetime.now().strftime("%H:%M:%S")
    terminal_statuses = {"success", "overdue", "refund", "refunded"}

    try:
        with Live(
            build_layout(tx, last_polled, poll_count),
            console=console,
            refresh_per_second=2,
            screen=True,
        ) as live:
            while True:
                current_status = tx.get("status", "")

                # Update display
                live.update(build_layout(tx, last_polled, poll_count))

                # Exit loop on terminal status
                if current_status in terminal_statuses:
                    # Show final state for a moment then prompt exit
                    time.sleep(3)
                    live.update(build_layout(tx, last_polled, poll_count))
                    # Give user time to read before Ctrl+C exits
                    if current_status == "success":
                        time.sleep(30)
                    else:
                        time.sleep(60)
                    break

                # Wait, then poll
                time.sleep(POLL_INTERVAL)

                try:
                    tx = get_transaction(args.api_key, tx_id)
                except requests.HTTPError as exc:
                    # Non-fatal: keep the old tx data, note the error
                    tx["_poll_error"] = str(exc)

                poll_count  += 1
                last_polled  = datetime.now().strftime("%H:%M:%S")

    except KeyboardInterrupt:
        pass

    # ── Post-TUI summary ──────────────────────────────────────────────────────
    console.print()
    console.print(Rule("[bold cyan]Final Status[/bold cyan]"))
    status       = tx.get("status", "unknown")
    color, label = STATUS_STYLES.get(status, ("white", status))
    console.print(Align.center(
        Text(f"\n{label}\n", style=f"bold {color}", justify="center")
    ))
    console.print(f"  [cyan]Transaction ID:[/]  {tx_id}")
    h_in  = (tx.get("hashIn")  or {}).get("hash") or "—"
    h_out = (tx.get("hashOut") or {}).get("hash") or "—"
    console.print(f"  [cyan]Hash In:[/]         {h_in}")
    console.print(f"  [cyan]Hash Out:[/]        {h_out}")
    console.print()

if __name__ == "__main__":
    main()