"""sous-chef CLI.

    sous-chef web                 — the planner at http://localhost:8766 (and your tailnet)
    sous-chef install-service     — keep it running in the background (macOS launchd)
    sous-chef mcp                 — the MCP server, for attaching to any Claude client
    sous-chef dedupe [--apply]    — merge recipes that were saved more than once
    sous-chef prices export FILE  — your price table as CSV, to edit in a spreadsheet
    sous-chef prices import FILE  — read it back
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import click
from rich.console import Console

console = Console()
DEFAULT_PORT = 8766   # Trainer has 8765; both can run side by side


@click.group()
def cli():
    """sous-chef — plan the week's meals with Claude."""


@cli.command()
@click.option("--host", help="Bind one specific address instead (e.g. 0.0.0.0). "
                            "Rarely needed — the default covers laptop and phone.")
@click.option("--port", default=DEFAULT_PORT, show_default=True)
@click.option("--no-tailnet", is_flag=True, help="Serve on this machine only")
@click.option("--reload", is_flag=True, help="Auto-reload on code changes")
def web(host: str | None, port: int, no_tailnet: bool, reload: bool):
    """Serve the planner.

    Binds loopback — which survives changing networks — plus your tailnet when
    Tailscale is up, so the laptop always works and the phone works when it can.
    """
    from sous_chef.storage.db import init_db
    from sous_chef.web.app import serve, serve_sockets
    from sous_chef.web.network import bind_sockets, resolve_host

    init_db()
    if host or reload:
        bind, _ = resolve_host(host or "127.0.0.1")
        console.print(f"[green]sous-chef at [cyan]http://{bind}:{port}[/cyan][/green]")
        if bind not in ("127.0.0.1", "localhost"):
            console.print("[yellow]⚠ Requests are still limited to this machine and your "
                          "tailnet.[/yellow]")
        serve(host=bind, port=port, reload=reload)
        return

    try:
        sockets, urls, tailnet_problem = bind_sockets(port, tailnet=not no_tailnet)
    except OSError as e:
        console.print(f"[red]✗ Could not listen on port {port}: {e}[/red]")
        console.print("[dim]Another copy may be running — [cyan]pkill -f 'sous-chef web'[/cyan][/dim]")
        raise SystemExit(1)

    console.print("[green]sous-chef is running[/green]")
    console.print(f"  [cyan]{urls[0]}[/cyan]  [dim]this laptop, on any network[/dim]")
    if len(urls) > 1:
        console.print(f"  [cyan]{urls[1]}[/cyan]  [dim]your phone, anywhere[/dim]")
    elif not no_tailnet:
        console.print(f"  [dim]No phone access — {tailnet_problem or 'Tailscale is not up'}.[/dim]")
        console.print("  [dim]The phone URL is picked up automatically once Tailscale signs in.[/dim]")
    serve_sockets(sockets, port=port, await_tailnet=len(urls) == 1 and not no_tailnet)


@cli.command("install-service")
@click.option("--port", default=DEFAULT_PORT, show_default=True)
@click.option("--remove", is_flag=True, help="Uninstall the service instead")
def install_service(port: int, remove: bool):
    """Keep `sous-chef web` running via launchd, restarting it after a reboot."""
    import plistlib
    import subprocess

    label = "com.sous-chef.web"
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"

    if remove:
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        if plist_path.exists():
            plist_path.unlink()
        console.print(f"[green]✓ Service removed[/green] [dim]({plist_path})[/dim]")
        return

    exe = str(Path(sys.executable).parent / "sous-chef")
    if not Path(exe).exists():
        console.print(f"[red]✗ Could not find the sous-chef executable at {exe}[/red]")
        raise SystemExit(1)

    from sous_chef.config import SOUS_CHEF_DIR
    SOUS_CHEF_DIR.mkdir(parents=True, exist_ok=True)
    log = SOUS_CHEF_DIR / "web.log"
    # launchd's PATH is /usr/bin:/bin:/usr/sbin:/sbin — not enough to find the
    # `claude` CLI the chef runs, so the current PATH goes into the plist.
    import os
    plist = {
        "Label": label,
        "ProgramArguments": [exe, "web", "--port", str(port)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    }
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    with open(plist_path, "wb") as fh:
        plistlib.dump(plist, fh)

    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    loaded = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)
    if loaded.returncode != 0:
        console.print(f"[yellow]⚠ Written to {plist_path}, but launchctl said: "
                      f"{loaded.stderr.strip()}[/yellow]")
        return
    console.print("[green]✓ sous-chef will start on login and restart if it stops[/green]")
    console.print(f"  [cyan]http://localhost:{port}[/cyan] [dim]— works on any network[/dim]")
    console.print(f"  [dim]logs: {log}[/dim]")
    console.print("  [dim]remove with [cyan]sous-chef install-service --remove[/cyan][/dim]")


@cli.command()
@click.option("--transport", default="stdio", show_default=True,
              type=click.Choice(["stdio", "sse", "streamable-http"]))
def mcp(transport: str):
    """Run the MCP server (the recipe chef's tools)."""
    from sous_chef.mcp.server import main as run_server
    run_server(transport=transport)


@cli.command()
@click.option("--apply", "do_apply", is_flag=True, help="Merge them (default: only list them)")
def dedupe(do_apply: bool):
    """Find recipes saved more than once and merge each set into one.

    The copy kept is one you chose in a plan if any, then one in your library,
    then the oldest. The others' places in plans move onto it.
    """
    from sous_chef import tools
    from sous_chef.storage.db import init_db
    init_db()
    groups = tools.merge_duplicate_recipes(apply=do_apply)
    if not groups:
        console.print("[green]✓ No duplicate recipes[/green]")
        return
    for g in groups:
        console.print(f"  {g['title']}  [dim]keep #{g['keep']}, "
                      f"{'merged' if do_apply else 'would merge'} "
                      f"{', '.join('#' + str(r) for r in g['remove'])}[/dim]")
    if do_apply:
        console.print(f"[green]✓ Merged {sum(len(g['remove']) for g in groups)} duplicate(s)[/green]")
    else:
        console.print("[dim]Run [cyan]sous-chef dedupe --apply[/cyan] to merge them.[/dim]")


@cli.group()
def prices():
    """Export or import the price table."""


FIELDS = ["ingredient_id", "ingredient", "store", "product", "pkg_qty", "pkg_unit", "price",
          "source", "carried"]


@prices.command("export")
@click.argument("path", type=click.Path(dir_okay=False))
@click.option("--store", help="Only this store (tj, qfc, pcc)")
def prices_export(path: str, store: str | None):
    """Write every offer to a CSV you can edit in Numbers or Excel."""
    from sous_chef import tools
    from sous_chef.storage.db import init_db
    init_db()
    rows = 0
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for ing in tools.search_catalog(limit=0):
            for o in ing["offers"]:
                if store and o["store"] != store:
                    continue
                w.writerow({"ingredient_id": ing["id"], "ingredient": ing["name"],
                            "store": o["store"], "product": o["product"],
                            "pkg_qty": o["pkg_qty"], "pkg_unit": o["pkg_unit"],
                            "price": o["price"], "source": o["source"],
                            "carried": "yes" if o["carried"] else "no"})
                rows += 1
    console.print(f"[green]✓ {rows} prices written to {path}[/green]")
    console.print("[dim]Edit price / package / carried, then [cyan]sous-chef prices import "
                  f"{path}[/cyan]. Only changed rows are recorded as yours.[/dim]")


@prices.command("import")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
def prices_import(path: str):
    """Read an edited CSV back. Rows that changed become your prices."""
    from sous_chef import tools
    from sous_chef.storage.db import init_db
    init_db()
    current = {(i["id"], o["store"]): o for i in tools.search_catalog(limit=0) for o in i["offers"]}
    changed = errors = 0
    with open(path, newline="") as fh:
        for n, row in enumerate(csv.DictReader(fh), start=2):
            key = (row["ingredient_id"], row["store"])
            old = current.get(key)
            try:
                price, qty = float(row["price"]), float(row["pkg_qty"])
                carried = row.get("carried", "yes").strip().lower() not in ("no", "n", "0", "false")
                if old and not carried and old["carried"]:
                    tools.set_carried(*key, False)
                    changed += 1
                    continue
                if (not old or price != old["price"] or qty != old["pkg_qty"]
                        or row["pkg_unit"] != old["pkg_unit"] or (carried and not old["carried"])):
                    tools.set_price(*key, price, qty, row["pkg_unit"], row.get("product") or None)
                    changed += 1
            except (ValueError, KeyError, tools.ToolError) as e:
                console.print(f"[yellow]line {n}: {e}[/yellow]")
                errors += 1
    console.print(f"[green]✓ {changed} price(s) updated[/green]"
                  + (f" [yellow]({errors} skipped)[/yellow]" if errors else ""))


if __name__ == "__main__":
    cli()
