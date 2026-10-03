"""
Rich TUI interface for bbackup.
Provides BTOP-like graphical interface for backup operations with live updates.
"""

import time
import threading
from typing import Any, List, Dict, Optional, Set, Callable
from datetime import timedelta
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import (
    Progress, SpinnerColumn, BarColumn, TextColumn, 
    TimeElapsedColumn, TimeRemainingColumn, MofNCompleteColumn
)
from rich.layout import Layout
from rich.live import Live
from rich.prompt import Confirm, Prompt
from rich import box

from . import __version__
from .config import Config, BackupSet
TUI_OPERATION_JOIN_TIMEOUT = 5.0


class BackupStatus:
    """Thread-safe status tracker for backup operations."""
    
    def __init__(self):
        self.lock = threading.Lock()
        self.current_action = "Initializing..."
        self.current_item = ""
        self.total_items = 0
        self.completed_items = 0
        self.start_time = None
        self.eta = None
        self.errors = []
        self.warnings = []
        self.status = "idle"  # idle, running, paused, cancelled, completed, error
        self.containers_status = {}
        self.volumes_status = {}
        self.networks_status = {}
        self.filesystems_status: Dict[str, Any] = {}
        self.remote_status = {}
        self.skip_current = False  # Flag to skip current item
        self.encryption_status = "idle"  # idle, encrypting, encrypted, failed
        
        # Transfer metrics
        self.bytes_transferred = 0  # Total bytes transferred
        self.total_bytes = 0  # Total bytes to transfer (if known)
        self.transfer_speed = 0.0  # Current transfer speed in MB/s
        self.files_transferred = 0  # Number of files transferred
        self.total_files = 0  # Total files to transfer (if known)
        self.current_file = ""  # Current file being processed
        self.last_update_time = None  # For speed calculation
        self.last_bytes = 0  # For speed calculation
    
    def update(self, action: str = None, item: str = None, 
               completed: int = None, total: int = None,
               bytes_transferred: int = None, total_bytes: int = None,
               files_transferred: int = None, total_files: int = None,
               current_file: str = None):
        """Update status (thread-safe)."""
        with self.lock:
            if action:
                self.current_action = action
            if item:
                self.current_item = item
            if completed is not None:
                self.completed_items = completed
            if total is not None:
                self.total_items = total
            if bytes_transferred is not None:
                self.bytes_transferred = bytes_transferred
            if total_bytes is not None:
                self.total_bytes = total_bytes
            if files_transferred is not None:
                self.files_transferred = files_transferred
            if total_files is not None:
                self.total_files = total_files
            if current_file is not None:
                self.current_file = current_file
            
            # Calculate transfer speed
            current_time = time.time()
            if self.last_update_time and self.bytes_transferred > self.last_bytes:
                time_delta = current_time - self.last_update_time
                bytes_delta = self.bytes_transferred - self.last_bytes
                if time_delta > 0:
                    # Calculate speed in MB/s
                    self.transfer_speed = (bytes_delta / time_delta) / (1024 * 1024)
            self.last_update_time = current_time
            self.last_bytes = self.bytes_transferred
            
            # Calculate ETA
            if self.start_time and self.completed_items > 0 and self.total_items > 0:
                elapsed = time.time() - self.start_time
                rate = self.completed_items / elapsed
                remaining = self.total_items - self.completed_items
                if rate > 0:
                    self.eta = timedelta(seconds=int(remaining / rate))
                else:
                    self.eta = None
            # Also calculate ETA based on transfer speed if we have bytes info
            elif self.transfer_speed > 0 and self.total_bytes > 0 and self.bytes_transferred < self.total_bytes:
                remaining_bytes = self.total_bytes - self.bytes_transferred
                remaining_seconds = remaining_bytes / (self.transfer_speed * 1024 * 1024)
                if remaining_seconds > 0:
                    self.eta = timedelta(seconds=int(remaining_seconds))
    
    def start(self):
        """Start timing without reviving a cancelled operation."""
        with self.lock:
            self.start_time = time.time()
            if self.status != "cancelled":
                self.status = "running"

    def get_status(self) -> str:
        """Return the current operation state."""
        with self.lock:
            return self.status

    def set_status(self, status: str, *, preserve_cancelled: bool = True) -> bool:
        """Set operation state, optionally preserving cancellation."""
        with self.lock:
            if preserve_cancelled and self.status == "cancelled" and status != "cancelled":
                return False
            self.status = status
            return True

    def consume_skip(self) -> bool:
        """Consume and clear a pending skip request atomically."""
        with self.lock:
            if not self.skip_current:
                return False
            self.skip_current = False
            return True
    def request_skip(self) -> None:
        """Request skipping the current item."""
        with self.lock:
            self.skip_current = True
    
    def cancel(self):
        """Cancel operation."""
        with self.lock:
            self.status = "cancelled"
    
    def add_error(self, error: str):
        """Add error message."""
        with self.lock:
            self.errors.append(error)
    
    def add_warning(self, warning: str):
        """Add warning message."""
        with self.lock:
            self.warnings.append(warning)
    def set_item_status(self, category: str, name: str, value: Any):
        """Update one item status while holding the status lock."""
        if category not in {"containers", "volumes", "networks", "filesystems", "remote"}:
            raise ValueError(f"Unknown status category: {category}")
        with self.lock:
            getattr(self, f"{category}_status")[name] = value

    def item_statuses(self, category: str) -> Dict[str, Any]:
        """Return a consistent copy of one item-status map."""
        if category not in {"containers", "volumes", "networks", "filesystems", "remote"}:
            raise ValueError(f"Unknown status category: {category}")
        with self.lock:
            return dict(getattr(self, f"{category}_status"))

    def messages(self) -> tuple[list[str], list[str]]:
        """Return copies of errors and warnings for rendering."""
        with self.lock:
            return list(self.errors), list(self.warnings)

    def set_encryption_status(self, status: str):
        """Set encryption state while holding the status lock."""
        with self.lock:
            self.encryption_status = status


class BackupTUI:
    """Terminal UI for backup operations with BTOP-like interface."""
    
    def __init__(self, config: Config):
        self.config = config
        self.console = Console()
        self.status = BackupStatus()
        self.cancelled = False
    
    def show_header(self, title: str = "bbackup - Docker Backup Tool"):
        """Display header panel."""
        header = Panel(
            f"[bold cyan]{title}[/bold cyan]\n"
            f"[dim]Version {__version__} - Docker Backup & Restore[/dim]",
            box=box.ROUNDED,
            border_style="cyan",
        )
        self.console.print(header)
    
    def create_live_dashboard(self) -> Layout:
        """Create live-updating dashboard layout (BTOP-like)."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=5),
            Layout(name="main", ratio=2),
            Layout(name="progress", size=8),
            Layout(name="status", size=6),
            Layout(name="footer", size=3),
        )
        
        layout["main"].split_row(
            Layout(name="containers", ratio=1),
            Layout(name="volumes", ratio=1),
            Layout(name="filesystems", ratio=1),
        )
        
        # Header
        current_status = self.status.get_status()
        elapsed = ""
        if self.status.start_time:
            elapsed_seconds = int(time.time() - self.status.start_time)
            elapsed = f" | Elapsed: {timedelta(seconds=elapsed_seconds)}"
        
        eta_str = ""
        if self.status.eta:
            eta_str = f" | ETA: {self.status.eta}"
        
        status_color = {
            "idle": "yellow",
            "running": "green",
            "paused": "yellow",
            "cancelled": "red",
            "completed": "green",
            "finalizing": "cyan",
        }.get(current_status, "white")
        
        # Transfer speed display
        speed_str = ""
        if self.status.transfer_speed > 0:
            if self.status.transfer_speed >= 1024:
                speed_str = f" | Speed: {self.status.transfer_speed/1024:.2f} GB/s"
            else:
                speed_str = f" | Speed: {self.status.transfer_speed:.2f} MB/s"
        
        # Bytes transferred display
        bytes_str = ""
        if self.status.bytes_transferred > 0:
            if self.status.bytes_transferred >= 1024**3:
                bytes_str = f" | Transferred: {self.status.bytes_transferred/(1024**3):.2f} GB"
            elif self.status.bytes_transferred >= 1024**2:
                bytes_str = f" | Transferred: {self.status.bytes_transferred/(1024**2):.2f} MB"
            else:
                bytes_str = f" | Transferred: {self.status.bytes_transferred/1024:.2f} KB"
        
        # Files transferred display
        files_str = ""
        if self.status.files_transferred > 0:
            if self.status.total_files > 0:
                files_str = f" | Files: {self.status.files_transferred}/{self.status.total_files}"
            else:
                files_str = f" | Files: {self.status.files_transferred}"
        
        header_content = f"""
[bold cyan]bbackup[/bold cyan] - Docker Backup Tool  [dim]v{__version__}[/dim]
Status: [{status_color}]{current_status.upper()}[/{status_color}]{elapsed}{eta_str}{speed_str}{bytes_str}{files_str}

[bold]Current:[/bold] {self.status.current_action}
[bold]Item:[/bold] {self.status.current_item if self.status.current_item else 'N/A'}
{('[bold]File:[/bold] ' + self.status.current_file[:60]) if self.status.current_file else ''}
"""
        layout["header"].update(Panel(header_content.strip(), border_style="cyan", box=box.ROUNDED))
        
        # Progress bar with enhanced metrics
        # Use bytes-based progress if available, otherwise use item-based
        if self.status.total_bytes > 0:
            # Bytes-based progress (more accurate for file transfers)
            progress_bar = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(bar_width=40),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TextColumn("•"),
                TextColumn("[cyan]{task.completed:>10}[/cyan]/[dim]{task.total:>10}[/dim]"),
                TextColumn("[dim]bytes[/dim]"),
                TextColumn("•"),
                TimeElapsedColumn(),
                TextColumn("•"),
                TimeRemainingColumn(),
            )
            total = self.status.total_bytes
            completed = self.status.bytes_transferred
        else:
            # Item-based progress (fallback)
            progress_bar = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(bar_width=40),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TextColumn("•"),
                MofNCompleteColumn(),
                TextColumn("•"),
                TimeElapsedColumn(),
            )
            
            # Add TimeRemainingColumn only if we have progress
            if self.status.total_items > 0 and self.status.completed_items > 0:
                progress_bar.columns = (
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(bar_width=40),
                    TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                    TextColumn("•"),
                    MofNCompleteColumn(),
                    TextColumn("•"),
                    TimeElapsedColumn(),
                    TextColumn("•"),
                    TimeRemainingColumn(),
                )
            total = self.status.total_items if self.status.total_items > 0 else None
            completed = self.status.completed_items
        
        progress_bar.add_task(
            self.status.current_action[:50] if self.status.current_action else "Processing...",
            total=total,
            completed=completed,
        )
        
        layout["progress"].update(
            Panel(progress_bar, title="Progress", border_style="blue", box=box.ROUNDED)
        )
        
        containers_status = self.status.item_statuses("containers")
        volumes_status = self.status.item_statuses("volumes")
        filesystems_status = self.status.item_statuses("filesystems")
        errors, warnings = self.status.messages()

        # Containers panel with enhanced info
        containers_table = Table(show_header=True, box=box.SIMPLE, show_edge=False)
        containers_table.add_column("Container", style="cyan", width=22)
        containers_table.add_column("Status", width=10)
        containers_table.add_column("Progress", style="dim", width=12)
        
        for name, status_info in list(containers_status.items())[:10]:
            # Handle both dict and string status
            if isinstance(status_info, dict):
                status = status_info.get("status", "unknown")
                size = status_info.get("size", "-")
                speed = status_info.get("speed", "")
            else:
                status = status_info
                size = "-"
                speed = ""
            
            status_color = "green" if status == "success" else "red" if status == "failed" else "yellow"
            progress_display = size if size != "-" else speed if speed else status
            containers_table.add_row(
                name[:22],
                f"[{status_color}]{status[:8]}[/{status_color}]",
                progress_display[:12],
            )
        
        if len(containers_status) == 0:
            containers_table.add_row("[dim]No containers backed up yet[/dim]", "", "")
        
        layout["containers"].update(
            Panel(containers_table, title="Containers", border_style="green", box=box.ROUNDED)
        )
        
        # Volumes panel with enhanced info
        volumes_table = Table(show_header=True, box=box.SIMPLE, show_edge=False)
        volumes_table.add_column("Volume", style="cyan", width=22)
        volumes_table.add_column("Status", width=10)
        volumes_table.add_column("Progress", style="dim", width=12)
        
        for name, status_info in list(volumes_status.items())[:10]:
            # Handle both dict and string status
            if isinstance(status_info, dict):
                status = status_info.get("status", "unknown")
                size = status_info.get("size", "-")
                speed = status_info.get("speed", "")
            else:
                status = status_info
                size = "-"
                speed = ""
            
            status_color = "green" if status == "success" else "red" if status == "failed" else "yellow"
            progress_display = size if size != "-" else speed if speed else status
            volumes_table.add_row(
                name[:22],
                f"[{status_color}]{status[:8]}[/{status_color}]",
                progress_display[:12],
            )
        
        if len(volumes_status) == 0:
            volumes_table.add_row("[dim]No volumes backed up yet[/dim]", "", "")
        
        layout["volumes"].update(
            Panel(volumes_table, title="Volumes", border_style="yellow", box=box.ROUNDED)
        )

        # Filesystems panel
        filesystems_table = Table(show_header=True, box=box.SIMPLE, show_edge=False)
        filesystems_table.add_column("Path", style="cyan", width=22)
        filesystems_table.add_column("Status", width=10)
        filesystems_table.add_column("Progress", style="dim", width=12)

        for name, status_info in list(filesystems_status.items())[:10]:
            if isinstance(status_info, dict):
                fs_status = status_info.get("status", "unknown")
                size = status_info.get("size", "-")
                speed = status_info.get("speed", "")
            else:
                fs_status = status_info
                size = "-"
                speed = ""
            color = "green" if fs_status == "success" else "red" if fs_status == "failed" else "yellow"
            progress_display = size if size != "-" else speed if speed else fs_status
            filesystems_table.add_row(
                name[:22],
                f"[{color}]{fs_status[:8]}[/{color}]",
                progress_display[:12],
            )

        if not filesystems_status:
            filesystems_table.add_row("[dim]No paths backed up yet[/dim]", "", "")

        layout["filesystems"].update(
            Panel(filesystems_table, title="Filesystems", border_style="cyan", box=box.ROUNDED)
        )

        # Status panel with encryption info and metrics
        status_lines = []
        
        # Show transfer metrics if available
        if self.status.transfer_speed > 0:
            if self.status.transfer_speed >= 1024:
                speed_display = f"{self.status.transfer_speed/1024:.2f} GB/s"
            else:
                speed_display = f"{self.status.transfer_speed:.2f} MB/s"
            status_lines.append(f"[cyan]⚡ Transfer Speed:[/cyan] {speed_display}")
        
        if self.status.bytes_transferred > 0:
            if self.status.bytes_transferred >= 1024**3:
                bytes_display = f"{self.status.bytes_transferred/(1024**3):.2f} GB"
            elif self.status.bytes_transferred >= 1024**2:
                bytes_display = f"{self.status.bytes_transferred/(1024**2):.2f} MB"
            else:
                bytes_display = f"{self.status.bytes_transferred/1024:.2f} KB"
            status_lines.append(f"[cyan]📦 Data Transferred:[/cyan] {bytes_display}")
        
        if self.status.files_transferred > 0:
            if self.status.total_files > 0:
                status_lines.append(f"[cyan]📄 Files:[/cyan] {self.status.files_transferred}/{self.status.total_files}")
            else:
                status_lines.append(f"[cyan]📄 Files:[/cyan] {self.status.files_transferred}")
        
        if hasattr(self.status, 'encryption_status'):
            if self.status.encryption_status == "encrypting":
                status_lines.append("[yellow]🔒 Encrypting backup...[/yellow]")
            elif self.status.encryption_status == "encrypted":
                status_lines.append("[green]🔒 Backup encrypted[/green]")
            elif self.status.encryption_status == "failed":
                status_lines.append("[red]🔒 Encryption failed[/red]")
        
        if errors:
            status_lines.append(f"[red]Errors: {len(errors)}[/red]")
            for error in errors[-2:]:  # Show last 2 errors
                status_lines.append(f"  [red]•[/red] {error[:55]}")
        if warnings:
            status_lines.append(f"[yellow]Warnings: {len(warnings)}[/yellow]")
            for warning in warnings[-2:]:  # Show last 2 warnings
                status_lines.append(f"  [yellow]•[/yellow] {warning[:55]}")
        if not status_lines:
            status_lines.append("[green]No errors or warnings[/green]")
        
        layout["status"].update(
            Panel("\n".join(status_lines), title="Status", border_style="magenta", box=box.ROUNDED)
        )
        
        # Footer with controls
        footer_content = """
[dim]Controls:[/dim] [bold]Q[/bold] = Quit/Cancel  [bold]P[/bold] = Pause  [bold]S[/bold] = Skip Current  [bold]H[/bold] = Help
"""
        layout["footer"].update(
            Panel(footer_content.strip(), border_style="dim", box=box.SIMPLE)
        )
        
        return layout
    
    @staticmethod
    def _set_terminal_cbreak(stream: Any) -> Any:
        """Put a terminal into cbreak mode and return its prior settings."""
        import termios
        import tty

        old_settings = termios.tcgetattr(stream)
        tty.setcbreak(stream.fileno())
        return old_settings

    @staticmethod
    def _restore_terminal(stream: Any, old_settings: Any) -> None:
        """Restore terminal settings captured by _set_terminal_cbreak."""
        import termios

        termios.tcsetattr(stream, termios.TCSADRAIN, old_settings)

    @staticmethod
    def _read_key(stream: Any) -> str:
        """Read one key while always restoring the terminal mode."""
        old_settings = BackupTUI._set_terminal_cbreak(stream)
        try:
            return stream.read(1)
        finally:
            BackupTUI._restore_terminal(stream, old_settings)

    def run_with_live_dashboard(self, operation: Callable, *args, **kwargs):
        """Run an operation while rendering a cancellable live dashboard."""
        import select
        import sys

        use_screen = sys.stdout.isatty() and sys.stdin.isatty()
        self.cancelled = False
        terminal_settings = None
        if sys.stdin.isatty():
            try:
                terminal_settings = self._set_terminal_cbreak(sys.stdin)
            except (ImportError, OSError, AttributeError, ValueError):
                terminal_settings = None

        def run_operation():
            try:
                operation(*args, **kwargs)
            except Exception as exc:
                self.status.add_error(str(exc))
                self.status.set_status("error")

        operation_thread = threading.Thread(
            target=run_operation,
            daemon=True,
        )
        operation_thread.start()

        try:
            with Live(
                self.create_live_dashboard(),
                refresh_per_second=4,
                screen=use_screen,
                console=self.console,
            ) as live:
                while operation_thread.is_alive() and self.status.get_status() not in {
                    "cancelled",
                    "error",
                }:
                    if terminal_settings is not None:
                        try:
                            if select.select([sys.stdin], [], [], 0.1)[0]:
                                key = sys.stdin.read(1)
                                if key.lower() == "q":
                                    self.status.cancel()
                                    self.cancelled = True
                                    break
                                if key.lower() == "p":
                                    current_status = self.status.get_status()
                                    if current_status == "running":
                                        self.status.set_status("paused")
                                    elif current_status == "paused":
                                        self.status.set_status("running")
                                elif key.lower() == "s":
                                    self.status.request_skip()
                                elif key.lower() == "h":
                                    self._show_help_screen()
                        except (ImportError, OSError, AttributeError, ValueError):
                            pass

                    live.update(self.create_live_dashboard())
                    time.sleep(0.25)

                live.update(self.create_live_dashboard())
        except KeyboardInterrupt:
            self.status.cancel()
            self.cancelled = True
        except Exception:
            if operation_thread.is_alive():
                self.status.cancel()
            raise
        finally:
            if terminal_settings is not None:
                try:
                    self._restore_terminal(sys.stdin, terminal_settings)
                except (OSError, ValueError):
                    self.status.add_error("Failed to restore terminal settings.")

            if operation_thread.is_alive() and self.status.get_status() not in {
                "cancelled",
                "error",
            }:
                self.status.cancel()
            operation_thread.join(timeout=TUI_OPERATION_JOIN_TIMEOUT)
            if operation_thread.is_alive():
                message = "Backup operation did not stop after cancellation."
                self.status.add_error(message)
                if self.status.get_status() != "cancelled":
                    self.status.set_status("error")

        return self.status.get_status() == "completed"
    
    def select_containers(self, containers: List[Dict]) -> Set[str]:
        """Interactive container selection."""
        self.console.print("\n[bold]Select Containers to Backup:[/bold]\n")
        
        # Create selection table
        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("ID", style="dim", width=12)
        table.add_column("Name", style="cyan", width=30)
        table.add_column("Status", width=12)
        table.add_column("Image", style="dim", width=30)
        
        for i, container in enumerate(containers, 1):
            status_color = "green" if container["status"] == "running" else "yellow"
            table.add_row(
                str(i),
                container["name"],
                f"[{status_color}]{container['status']}[/{status_color}]",
                container["image"][:30],
            )
        
        self.console.print(table)
        
        # Get selection
        self.console.print("\n[dim]Enter container numbers (comma-separated) or 'all' for all containers:[/dim]")
        selection = Prompt.ask("Selection", default="all", console=self.console)
        
        if selection.lower() == "all":
            return {c["name"] for c in containers}
        
        try:
            indices = [int(x.strip()) - 1 for x in selection.split(",")]
            selected = {containers[i]["name"] for i in indices if 0 <= i < len(containers)}
            return selected
        except (ValueError, IndexError):
            self.console.print("[red]Invalid selection, using all containers[/red]")
            return {c["name"] for c in containers}
    
    def select_backup_set(self) -> Optional[BackupSet]:
        """Select backup set from configuration."""
        if not self.config.backup_sets:
            return None
        
        self.console.print("\n[bold]Available Backup Sets:[/bold]\n")
        
        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Name", style="cyan", width=20)
        table.add_column("Description", width=40)
        table.add_column("Containers", style="dim", width=30)
        
        sets_list = list(self.config.backup_sets.values())
        for i, backup_set in enumerate(sets_list, 1):
            containers_str = ", ".join(backup_set.containers[:3])
            if len(backup_set.containers) > 3:
                containers_str += f" (+{len(backup_set.containers) - 3} more)"
            table.add_row(
                str(i),
                backup_set.description or backup_set.name,
                containers_str,
            )
        
        self.console.print(table)
        
        self.console.print("\n[dim]Select backup set number, or press Enter to skip:[/dim]")
        selection = Prompt.ask("Selection", default="", console=self.console)
        
        if not selection:
            return None
        
        try:
            index = int(selection.strip()) - 1
            if 0 <= index < len(sets_list):
                return sets_list[index]
        except ValueError:
            pass
        
        return None
    
    def select_scope(self) -> Dict[str, bool]:
        """Select backup scope."""
        self.console.print("\n[bold]Select Backup Scope:[/bold]\n")
        
        scope = {
            "containers": Confirm.ask(
                "Backup container configurations?", default=True, console=self.console
            ),
            "volumes": Confirm.ask(
                "Backup data volumes?", default=True, console=self.console
            ),
            "networks": Confirm.ask(
                "Backup network configurations?", default=True, console=self.console
            ),
            "configs": Confirm.ask(
                "Backup container configs/metadata?", default=True, console=self.console
            ),
        }
        
        return scope
    
    def _show_help_screen(self):
        """Display help screen with keyboard shortcuts."""
        help_content = """
[bold cyan]bbackup - Keyboard Controls[/bold cyan]

[bold]Q[/bold] - Quit/Cancel backup
  Cancels the current backup operation and exits

[bold]P[/bold] - Pause/Resume backup
  Pauses or resumes the backup operation

[bold]S[/bold] - Skip current item
  Skips the current container/volume/network being backed up

[bold]H[/bold] - Help (this screen)
  Shows this help screen

 [dim]Help is shown while the backup continues.[/dim]
"""
        from rich.panel import Panel
        self.console.print(Panel(help_content.strip(), title="Help", border_style="cyan", box=box.ROUNDED))
        # In the live dashboard, this would be shown in a modal/overlay
        # For now, it's printed to console
    
    def show_backup_status(self, results: Dict, errors: List[str]):
        """Display backup results."""
        self.console.print("\n[bold]Backup Results:[/bold]\n")
        
        # Success summary
        table = Table(show_header=True, header_style="bold green", box=box.ROUNDED)
        table.add_column("Type", style="cyan", width=15)
        table.add_column("Success", style="green", width=10)
        table.add_column("Failed", style="red", width=10)
        table.add_column("Skipped", style="yellow", width=10)

        def counts(values: Dict[str, Any]) -> tuple[int, int, int]:
            return (
                sum(1 for value in values.values() if value == "success"),
                sum(1 for value in values.values() if value == "failed"),
                sum(1 for value in values.values() if value == "skipped"),
            )

        for label, key in (
            ("Containers", "containers"),
            ("Volumes", "volumes"),
            ("Networks", "networks"),
            ("Filesystems", "filesystems"),
            ("Remotes", "remotes"),
        ):
            values = results.get(key, {})
            if key == "remotes" and not values:
                continue
            table.add_row(label, *(str(value) for value in counts(values)))
        
        self.console.print(table)
        
        # Errors
        if errors:
            self.console.print("\n[bold red]Errors:[/bold red]")
            for error in errors:
                self.console.print(f"  [red]•[/red] {error}")
