"""Interface grafica do iOS Location Studio."""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

import customtkinter as ctk
from tkintermapview import TkinterMapView

from . import APP_NAME, APP_VERSION, device, driver, geo
from .config import Store

ACCENT = "#2f81f7"
ACCENT_HOVER = "#1f6feb"
DANGER = "#e5534b"
DANGER_HOVER = "#c93c33"
OK = "#3fb950"
WARN = "#d29922"
MUTED = "#8b949e"
CARD = "#161b22"
CARD_BORDER = "#26303d"
SURFACE = "#0d1117"

TILE_SERVERS = {
    "Mapa (OpenStreetMap)": ("https://a.tile.openstreetmap.org/{z}/{x}/{y}.png", 19),
    "Satelite (Esri)": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        19,
    ),
}

POLL_MS = 120
MOVE_TICK_MS = 1000


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title(f"{APP_NAME} {APP_VERSION}")
        width, height = self._fit_to_screen(1320, 840)
        self.geometry(f"{width}x{height}")
        self.minsize(940, 560)
        self.configure(fg_color=SURFACE)

        self.store = Store()
        self.worker = device.DeviceWorker()
        self.worker.start()

        self._bg_results: queue.Queue[tuple[Callable[[bool, Any], None], tuple[bool, Any]]] = queue.Queue()
        self._devices: list[dict[str, Any]] = []
        self._state = device.STATE_DISCONNECTED
        self._simulating = False
        self._device_info: device.DeviceInfo | None = None
        self._selected: tuple[float, float] = tuple(self.store.get("last_position"))  # type: ignore[assignment]
        self._search_results: list[geo.Place] = []

        self._route: list[tuple[float, float]] = []
        self._route_path: Any = None
        self._route_index = 0
        self._route_running = False
        self._route_cursor: tuple[float, float] | None = None
        self._move_job: str | None = None
        self._tunnel_attempts = 0
        self._driver_installing = False
        self._async_logs: queue.Queue[tuple[str, str]] = queue.Queue()

        self._build_layout()
        self._center_on_start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(POLL_MS, self._poll)
        self.refresh_devices()
        self._check_environment()

    def _fit_to_screen(self, wanted_width: int, wanted_height: int) -> tuple[int, int]:
        """Tamanho da janela sem estourar a tela.

        O CustomTkinter multiplica a geometria pelo seu fator de escala, que pode
        diferir de 1 em telas com DPI alto.
        """
        try:
            scaling = max(0.5, float(ctk.ScalingTracker.get_window_scaling(self)))
        except Exception:  # noqa: BLE001
            scaling = 1.0
        available_width = self.winfo_screenwidth() / scaling - 60
        available_height = self.winfo_screenheight() / scaling - 110
        return int(min(wanted_width, available_width)), int(min(wanted_height, available_height))

    # ------------------------------------------------------------------- layout
    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=0, minsize=380)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_sidebar()
        self._build_map()
        self._build_footer()

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=72)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        header.grid_propagate(False)

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.grid(row=0, column=0, padx=20, pady=12, sticky="w")
        ctk.CTkLabel(
            title_box,
            text=APP_NAME,
            font=ctk.CTkFont(size=19, weight="bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_box,
            text="Simulacao de GPS no iPhone em modo desenvolvedor",
            font=ctk.CTkFont(size=11),
            text_color=MUTED,
        ).pack(anchor="w")

        controls = ctk.CTkFrame(header, fg_color="transparent")
        controls.grid(row=0, column=2, padx=20, pady=12, sticky="e")

        self.status_pill = ctk.CTkLabel(
            controls,
            text="  Desconectado  ",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#30363d",
            corner_radius=14,
            height=28,
        )
        self.status_pill.pack(side="left", padx=(0, 14))

        self.device_menu = ctk.CTkOptionMenu(
            controls,
            values=["Nenhum aparelho"],
            width=210,
            height=34,
            fg_color="#21262d",
            button_color="#30363d",
            button_hover_color="#3d444d",
        )
        self.device_menu.pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            controls,
            text="Atualizar",
            width=90,
            height=34,
            fg_color="#21262d",
            hover_color="#30363d",
            command=self.refresh_devices,
        ).pack(side="left", padx=(0, 8))

        self.connect_button = ctk.CTkButton(
            controls,
            text="Conectar",
            width=120,
            height=34,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.toggle_connection,
        )
        self.connect_button.pack(side="left")

    def _build_sidebar(self) -> None:
        wrapper = ctk.CTkFrame(self, fg_color="transparent")
        wrapper.grid(row=1, column=0, sticky="nsew", padx=(14, 7), pady=14)
        wrapper.grid_rowconfigure(0, weight=1)
        wrapper.grid_columnconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(wrapper, fg_color="transparent")
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        self._build_device_card(scroll)
        self._build_movement_card(scroll)
        self._build_favorites_card(scroll)

        # a acao principal fica fora do scroll, sempre visivel
        self._build_coordinates_card(wrapper).grid(row=1, column=0, sticky="ew", pady=(12, 0))

    def _card(self, parent: Any, title: str, subtitle: str = "", pack: bool = True) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14, border_width=1, border_color=CARD_BORDER)
        if pack:
            card.pack(fill="x", pady=(0, 12))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card,
            text=title.upper(),
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=MUTED,
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 0))
        if subtitle:
            ctk.CTkLabel(
                card,
                text=subtitle,
                font=ctk.CTkFont(size=11),
                text_color=MUTED,
                wraplength=300,
                justify="left",
            ).grid(row=1, column=0, sticky="w", padx=16, pady=(2, 0))
        return card

    def _build_device_card(self, parent: Any) -> None:
        card = self._card(parent, "Aparelho")
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.grid(row=2, column=0, sticky="ew", padx=16, pady=(8, 14))
        body.grid_columnconfigure(1, weight=1)

        body.grid_columnconfigure(3, weight=1)

        self.info_labels: dict[str, ctk.CTkLabel] = {}
        cells = (
            ("name", "Nome", 0, 0),
            ("ios", "iOS", 0, 2),
            ("mode", "Modo dev", 1, 0),
            ("tunnel", "Tunel", 1, 2),
            ("link", "Canal", 2, 0),
            ("apple", "Driver", 2, 2),
        )
        for key, label, row, column in cells:
            ctk.CTkLabel(
                body, text=label, font=ctk.CTkFont(size=11), text_color=MUTED, anchor="w"
            ).grid(row=row, column=column, sticky="w", pady=1)
            value = ctk.CTkLabel(body, text="-", font=ctk.CTkFont(size=12), anchor="w")
            value.grid(row=row, column=column + 1, sticky="ew", padx=(8, 12), pady=1)
            self.info_labels[key] = value

        self.tunnel_button = ctk.CTkButton(
            card,
            text="Iniciar tunel RSD (admin) - iOS 17+",
            height=32,
            fg_color="#21262d",
            hover_color="#30363d",
            command=self.start_tunnel,
        )
        self.tunnel_button.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 8))

        self.driver_button = ctk.CTkButton(
            card,
            text="Instalar driver Apple (sem iTunes)",
            height=32,
            fg_color="#21262d",
            hover_color="#30363d",
            command=self.install_driver,
        )
        self.driver_button.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 14))

        self.info_labels["tunnel"].configure(text="verificando...")
        self.info_labels["apple"].configure(text="verificando...")

    def _build_coordinates_card(self, parent: Any) -> ctk.CTkFrame:
        card = self._card(parent, "Localizacao", "Clique no mapa ou digite as coordenadas", pack=False)
        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.grid(row=2, column=0, sticky="ew", padx=16, pady=(10, 6))
        grid.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkLabel(grid, text="Latitude", font=ctk.CTkFont(size=11), text_color=MUTED).grid(
            row=0, column=0, sticky="w"
        )
        ctk.CTkLabel(grid, text="Longitude", font=ctk.CTkFont(size=11), text_color=MUTED).grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )
        self.lat_entry = ctk.CTkEntry(grid, height=34)
        self.lat_entry.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.lon_entry = ctk.CTkEntry(grid, height=34)
        self.lon_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(2, 0))

        self.place_label = ctk.CTkLabel(
            card,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=MUTED,
            wraplength=300,
            justify="left",
        )
        self.place_label.grid(row=3, column=0, sticky="w", padx=16, pady=(8, 0))

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=4, column=0, sticky="ew", padx=16, pady=(10, 8))
        buttons.grid_columnconfigure((0, 1), weight=1)

        self.apply_button = ctk.CTkButton(
            buttons,
            text="Aplicar no iPhone",
            height=38,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.apply_location,
        )
        self.apply_button.grid(row=0, column=0, columnspan=2, sticky="ew")

        self.clear_button = ctk.CTkButton(
            buttons,
            text="Remover simulacao",
            height=34,
            fg_color=DANGER,
            hover_color=DANGER_HOVER,
            command=self.clear_location,
        )
        self.clear_button.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        extra = ctk.CTkFrame(card, fg_color="transparent")
        extra.grid(row=5, column=0, sticky="ew", padx=16, pady=(0, 14))
        extra.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(
            extra,
            text="Usar centro do mapa",
            height=30,
            fg_color="#21262d",
            hover_color="#30363d",
            command=self.use_map_center,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ctk.CTkButton(
            extra,
            text="Salvar favorito",
            height=30,
            fg_color="#21262d",
            hover_color="#30363d",
            command=self.save_favorite,
        ).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        return card

    def _build_movement_card(self, parent: Any) -> None:
        card = self._card(
            parent,
            "Movimento",
            "Botao direito no mapa adiciona pontos de rota; o app caminha entre eles",
        )
        self.route_label = ctk.CTkLabel(
            card, text="Nenhum ponto de rota", font=ctk.CTkFont(size=12), text_color=MUTED
        )
        self.route_label.grid(row=2, column=0, sticky="w", padx=16, pady=(10, 4))

        speed_row = ctk.CTkFrame(card, fg_color="transparent")
        speed_row.grid(row=3, column=0, sticky="ew", padx=16)
        speed_row.grid_columnconfigure(0, weight=1)

        self.speed_value = float(self.store.get("speed_kmh", 40.0))
        self.speed_label = ctk.CTkLabel(
            speed_row, text=f"Velocidade: {self.speed_value:.0f} km/h", font=ctk.CTkFont(size=12)
        )
        self.speed_label.grid(row=0, column=0, sticky="w")
        self.speed_slider = ctk.CTkSlider(
            speed_row, from_=1, to=200, number_of_steps=199, command=self._on_speed_change
        )
        self.speed_slider.set(self.speed_value)
        self.speed_slider.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=4, column=0, sticky="ew", padx=16, pady=(12, 14))
        buttons.grid_columnconfigure((0, 1), weight=1)
        self.move_button = ctk.CTkButton(
            buttons,
            text="Iniciar percurso",
            height=32,
            fg_color="#21262d",
            hover_color="#30363d",
            command=self.toggle_route,
        )
        self.move_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ctk.CTkButton(
            buttons,
            text="Limpar rota",
            height=32,
            fg_color="#21262d",
            hover_color="#30363d",
            command=self.clear_route,
        ).grid(row=0, column=1, sticky="ew", padx=(4, 0))

    def _build_favorites_card(self, parent: Any) -> None:
        card = self._card(parent, "Favoritos")
        self.favorites_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.favorites_frame.grid(row=2, column=0, sticky="ew", padx=16, pady=(10, 14))
        self.favorites_frame.grid_columnconfigure(0, weight=1)
        self._render_favorites()

    def _build_map(self) -> None:
        container = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14, border_width=1, border_color=CARD_BORDER)
        container.grid(row=1, column=1, sticky="nsew", padx=(7, 14), pady=14)
        container.grid_rowconfigure(1, weight=1)
        container.grid_columnconfigure(0, weight=1)

        toolbar = ctk.CTkFrame(container, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))
        toolbar.grid_columnconfigure(1, weight=1)

        self.tile_menu = ctk.CTkOptionMenu(
            toolbar,
            values=list(TILE_SERVERS),
            width=180,
            height=34,
            fg_color="#21262d",
            button_color="#30363d",
            button_hover_color="#3d444d",
            command=self._on_tile_change,
        )
        self.tile_menu.grid(row=0, column=0, sticky="w")

        search_box = ctk.CTkFrame(toolbar, fg_color="transparent")
        search_box.grid(row=0, column=1, sticky="ew", padx=10)
        search_box.grid_columnconfigure(0, weight=1)

        self.search_entry = ctk.CTkEntry(
            search_box,
            placeholder_text="Buscar endereco, cidade ou 'lat, lon'",
            height=34,
        )
        self.search_entry.grid(row=0, column=0, sticky="ew")
        self.search_entry.bind("<Return>", lambda _event: self.search_place())

        ctk.CTkButton(
            search_box,
            text="Buscar",
            width=84,
            height=34,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self.search_place,
        ).grid(row=0, column=1, padx=(8, 0))

        ctk.CTkButton(
            toolbar,
            text="Centralizar no pino",
            width=150,
            height=34,
            fg_color="#21262d",
            hover_color="#30363d",
            command=lambda: self.map_widget.set_position(*self._selected),
        ).grid(row=0, column=2, sticky="e")

        self.map_widget = TkinterMapView(container, corner_radius=10)
        self.map_widget.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

        # resultados da busca aparecem flutuando sobre o mapa, abaixo da barra
        self.results_frame = ctk.CTkFrame(
            container, fg_color=CARD, corner_radius=10, border_width=1, border_color=CARD_BORDER
        )
        self.results_frame.grid_columnconfigure(0, weight=1)

        self.map_hint = ctk.CTkLabel(
            container,
            text="  Clique no mapa para escolher o ponto  ",
            font=ctk.CTkFont(size=11),
            text_color=MUTED,
            fg_color=CARD,
            corner_radius=10,
            height=26,
        )
        self.map_hint.place(x=24, rely=1.0, y=-26, anchor="sw")
        self._on_tile_change(self.tile_menu.get())
        self.map_widget.add_left_click_map_command(self._on_map_click)
        self.map_widget.add_right_click_menu_command(
            label="Definir localizacao aqui", command=self._on_map_click, pass_coords=True
        )
        self.map_widget.add_right_click_menu_command(
            label="Adicionar ponto de rota", command=self._add_route_point, pass_coords=True
        )

        self.pin = self.map_widget.set_marker(
            self._selected[0],
            self._selected[1],
            text="Alvo",
            marker_color_circle="#0b3d91",
            marker_color_outside=ACCENT,
            text_color="#ffffff",
        )

    def _build_footer(self) -> None:
        footer = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=124)
        footer.grid(row=2, column=0, columnspan=2, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        footer.grid_propagate(False)

        self.log_box = ctk.CTkTextbox(
            footer, height=80, fg_color=SURFACE, font=ctk.CTkFont(family="Consolas", size=12)
        )
        self.log_box.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 4))
        self.log_box.configure(state="disabled")

        self.status_bar = ctk.CTkLabel(
            footer,
            text="Pronto.",
            font=ctk.CTkFont(size=11),
            text_color=MUTED,
            anchor="w",
        )
        self.status_bar.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))

    # -------------------------------------------------------------------- helpers
    def log(self, message: str, level: str = "info") -> None:
        prefix = {"ok": "[ ok ]", "warn": "[aviso]", "error": "[erro]", "info": "[info]"}.get(
            level, "[info]"
        )
        stamp = time.strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{stamp} {prefix} {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        self.status_bar.configure(text=message)

    def _log_from_thread(self, message: str, level: str = "info") -> None:
        """Log seguro para chamadas fora da thread da interface."""
        self._async_logs.put((message, level))

    def _run_bg(self, task: Callable[[], Any], on_done: Callable[[bool, Any], None]) -> None:
        def runner() -> None:
            try:
                self._bg_results.put((on_done, (True, task())))
            except Exception as exc:  # noqa: BLE001
                self._bg_results.put((on_done, (False, exc)))

        threading.Thread(target=runner, daemon=True).start()

    def _selected_udid(self) -> str | None:
        label = self.device_menu.get()
        for entry in self._devices:
            if entry["label"] == label:
                return entry["udid"]
        return None

    def _set_selected(self, latitude: float, longitude: float, describe: bool = True) -> None:
        self._selected = (latitude, longitude)
        self.lat_entry.delete(0, "end")
        self.lat_entry.insert(0, f"{latitude:.6f}")
        self.lon_entry.delete(0, "end")
        self.lon_entry.insert(0, f"{longitude:.6f}")
        self.pin.set_position(latitude, longitude)
        self.store.set("last_position", [latitude, longitude])
        if describe:
            self.place_label.configure(text="Buscando nome do local...")
            self._run_bg(lambda: geo.reverse(latitude, longitude), self._on_reverse_done)

    def _on_reverse_done(self, ok: bool, value: Any) -> None:
        self.place_label.configure(text=value if ok and value else "")

    def _read_entries(self) -> tuple[float, float] | None:
        try:
            latitude = float(self.lat_entry.get().strip().replace(",", "."))
            longitude = float(self.lon_entry.get().strip().replace(",", "."))
        except ValueError:
            self.log("Latitude e longitude precisam ser numeros.", "error")
            return None
        if not geo.is_valid(latitude, longitude):
            self.log("Coordenadas fora da faixa valida (-90..90 e -180..180).", "error")
            return None
        return latitude, longitude

    def _center_on_start(self) -> None:
        self.map_widget.set_position(*self._selected)
        self.map_widget.set_zoom(int(self.store.get("last_zoom", 13)))
        self._set_selected(*self._selected)

        if self.store.get("last_position") == [-23.5613, -46.6565]:
            self._run_bg(geo.approximate_self_location, self._on_self_location)

    def _on_self_location(self, ok: bool, value: Any) -> None:
        if ok and value:
            self.map_widget.set_position(*value)
            self._set_selected(*value)
            self.log(
                "Mapa centralizado na localizacao aproximada do computador (por IP). "
                "O iPhone nao expoe a leitura do GPS real por este protocolo.",
                "info",
            )

    # ------------------------------------------------------------------- aparelho
    def refresh_devices(self) -> None:
        self.log("Procurando iPhones conectados...")
        self.worker.submit("list_devices")

    def toggle_connection(self) -> None:
        if self._state == device.STATE_CONNECTED:
            self.stop_route()
            self.worker.submit("disconnect")
            return
        self._tunnel_attempts = 0
        self._connect()

    def _connect(self) -> None:
        udid = self._selected_udid()
        if udid is None:
            self.log("Selecione um aparelho na lista (ou clique em Atualizar).", "warn")
            return
        self.store.set("last_udid", udid)
        self.log("Conectando ao aparelho...")
        self.worker.submit("connect", udid=udid)

    def start_tunnel(self) -> None:
        self.log(device.start_tunneld_elevated(), "info")
        self._wait_for_tunnel(reconnect=False)

    def _on_tunnel_required(self) -> None:
        """iOS 17+ sem tunel: tenta subir o tunneld e reconectar sozinho."""
        if self._tunnel_attempts >= 1:
            self.log(
                "O tunel continua indisponivel. Rode em um terminal como administrador: "
                "python -m pymobiledevice3 remote tunneld",
                "warn",
            )
            return
        self._tunnel_attempts += 1
        self.log(device.start_tunneld_elevated(), "info")
        self._wait_for_tunnel()

    def _wait_for_tunnel(self, attempt: int = 0, reconnect: bool = True) -> None:
        def done(ok: bool, running: Any) -> None:
            if ok and running:
                self.info_labels["tunnel"].configure(text="ativo")
                self.log("Tunel RSD ativo.", "ok")
                if reconnect:
                    self._connect()
                return
            if attempt < 15:
                self.after(2000, lambda: self._wait_for_tunnel(attempt + 1, reconnect))
            else:
                self.log("O tunel nao subiu. Verifique se o UAC foi aceito.", "warn")

        self._run_bg(device.tunneld_is_running, done)

    def _check_environment(self, repeat: bool = True) -> None:
        """Atualiza o estado do tunel RSD e do driver da Apple."""

        def tunnel_done(ok: bool, running: Any) -> None:
            self.info_labels["tunnel"].configure(text="ativo" if ok and running else "inativo")

        def driver_done(ok: bool, available: Any) -> None:
            self.info_labels["apple"].configure(text="ok" if ok and available else "ausente")

        self._run_bg(device.tunneld_is_running, tunnel_done)
        self._run_bg(driver.usbmux_available, driver_done)
        if repeat:
            self.after(8000, self._check_environment)

    def install_driver(self) -> None:
        """Instala o suporte a dispositivos Apple sem passar pelo iTunes."""
        if self._driver_installing:
            self.log("A instalacao do driver ja esta em andamento.", "warn")
            return
        self._driver_installing = True
        self.driver_button.configure(state="disabled", text="Instalando driver...")

        def done(ok: bool, value: Any) -> None:
            self._driver_installing = False
            self.driver_button.configure(state="normal", text="Instalar driver Apple (sem iTunes)")
            if not ok:
                self.log(f"Falha na instalacao do driver: {value}", "error")
                return
            self._check_environment(repeat=False)
            if value:
                self.refresh_devices()

        self._run_bg(lambda: driver.install_apple_device_support(self._log_from_thread), done)

    # ------------------------------------------------------------------ localizacao
    def apply_location(self) -> None:
        coords = self._read_entries()
        if coords is None:
            return
        if self._state != device.STATE_CONNECTED:
            self.log("Conecte um iPhone antes de aplicar a localizacao.", "warn")
            return
        self._set_selected(*coords)
        self.worker.request_location(*coords)

    def clear_location(self) -> None:
        self.stop_route()
        self.worker.submit("clear_location")

    def use_map_center(self) -> None:
        latitude, longitude = self.map_widget.get_position()
        self._set_selected(latitude, longitude)

    def search_place(self) -> None:
        query = self.search_entry.get().strip()
        if not query:
            return
        self.log(f"Buscando '{query}'...")
        self._run_bg(lambda: geo.search(query), self._on_search_done)

    def _on_search_done(self, ok: bool, value: Any) -> None:
        self._hide_results()
        for child in self.results_frame.winfo_children():
            child.destroy()
        if not ok:
            self.log(f"Busca falhou: {value}", "error")
            return
        self._search_results = value
        if not self._search_results:
            self.log("Nenhum resultado encontrado.", "warn")
            return
        for index, place in enumerate(self._search_results[:6]):
            label = place.label if len(place.label) <= 64 else place.label[:61] + "..."
            ctk.CTkButton(
                self.results_frame,
                text=label,
                anchor="w",
                width=440,
                height=30,
                fg_color="#21262d",
                hover_color="#30363d",
                font=ctk.CTkFont(size=11),
                command=lambda i=index: self._choose_result(i),
            ).grid(row=index, column=0, sticky="ew", padx=8, pady=(8 if index == 0 else 2, 0))
        ctk.CTkFrame(self.results_frame, fg_color="transparent", height=8).grid(
            row=len(self._search_results[:6]), column=0
        )
        self.results_frame.place(x=205, y=54)
        self.results_frame.lift()
        self.log(f"{len(self._search_results)} resultado(s).", "ok")

    def _hide_results(self) -> None:
        self.results_frame.place_forget()

    def _choose_result(self, index: int) -> None:
        place = self._search_results[index]
        self._hide_results()
        self._set_selected(place.lat, place.lon, describe=False)
        self.place_label.configure(text=place.label)
        self.map_widget.set_position(place.lat, place.lon)
        self.map_widget.set_zoom(15)

    def _on_map_click(self, coords: tuple[float, float]) -> None:
        self._hide_results()
        self._set_selected(coords[0], coords[1])

    def _on_tile_change(self, choice: str) -> None:
        url, max_zoom = TILE_SERVERS[choice]
        self.map_widget.set_tile_server(url, max_zoom=max_zoom)

    # -------------------------------------------------------------------- favoritos
    def save_favorite(self) -> None:
        coords = self._read_entries()
        if coords is None:
            return
        dialog = ctk.CTkInputDialog(text="Nome do favorito:", title="Salvar favorito")
        name = (dialog.get_input() or "").strip()
        if not name:
            return
        self.store.add_favorite(name, *coords)
        self._render_favorites()
        self.log(f"Favorito '{name}' salvo.", "ok")

    def _render_favorites(self) -> None:
        for child in self.favorites_frame.winfo_children():
            child.destroy()
        favorites = self.store.favorites
        if not favorites:
            ctk.CTkLabel(
                self.favorites_frame,
                text="Nenhum favorito salvo ainda.",
                font=ctk.CTkFont(size=11),
                text_color=MUTED,
            ).grid(row=0, column=0, sticky="w")
            return
        for index, favorite in enumerate(favorites):
            row = ctk.CTkFrame(self.favorites_frame, fg_color="transparent")
            row.grid(row=index, column=0, sticky="ew", pady=2)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkButton(
                row,
                text=favorite.get("name", "?"),
                anchor="w",
                height=30,
                fg_color="#21262d",
                hover_color="#30363d",
                font=ctk.CTkFont(size=12),
                command=lambda i=index: self._use_favorite(i),
            ).grid(row=0, column=0, sticky="ew")
            ctk.CTkButton(
                row,
                text="X",
                width=30,
                height=30,
                fg_color="#21262d",
                hover_color=DANGER_HOVER,
                command=lambda i=index: self._delete_favorite(i),
            ).grid(row=0, column=1, padx=(6, 0))

    def _use_favorite(self, index: int) -> None:
        favorite = self.store.favorites[index]
        latitude, longitude = float(favorite["lat"]), float(favorite["lon"])
        self._set_selected(latitude, longitude, describe=False)
        self.place_label.configure(text=favorite.get("name", ""))
        self.map_widget.set_position(latitude, longitude)

    def _delete_favorite(self, index: int) -> None:
        self.store.remove_favorite(index)
        self._render_favorites()

    # --------------------------------------------------------------------- rota
    def _on_speed_change(self, value: float) -> None:
        self.speed_value = float(value)
        self.speed_label.configure(text=f"Velocidade: {self.speed_value:.0f} km/h")
        self.store.set("speed_kmh", self.speed_value)

    def _add_route_point(self, coords: tuple[float, float]) -> None:
        self._route.append((coords[0], coords[1]))
        self.map_widget.set_marker(
            coords[0],
            coords[1],
            text=str(len(self._route)),
            marker_color_circle="#1b3a2a",
            marker_color_outside=OK,
            text_color="#ffffff",
        )
        self._redraw_route()
        distance = geo.path_length_m(self._route)
        self.route_label.configure(
            text=f"{len(self._route)} ponto(s) - {distance / 1000:.2f} km de percurso"
        )

    def _redraw_route(self) -> None:
        if self._route_path is not None:
            self._route_path.delete()
            self._route_path = None
        if len(self._route) >= 2:
            self._route_path = self.map_widget.set_path(list(self._route), color=OK, width=4)

    def clear_route(self) -> None:
        self.stop_route()
        self._route.clear()
        self._route_index = 0
        self._route_cursor = None
        if self._route_path is not None:
            self._route_path.delete()
            self._route_path = None
        self.map_widget.delete_all_marker()
        self.pin = self.map_widget.set_marker(
            self._selected[0],
            self._selected[1],
            text="Alvo",
            marker_color_circle="#0b3d91",
            marker_color_outside=ACCENT,
            text_color="#ffffff",
        )
        self.route_label.configure(text="Nenhum ponto de rota")

    def toggle_route(self) -> None:
        if self._route_running:
            self.stop_route()
            return
        if len(self._route) < 2:
            self.log("Adicione ao menos 2 pontos de rota (botao direito no mapa).", "warn")
            return
        if self._state != device.STATE_CONNECTED:
            self.log("Conecte um iPhone antes de iniciar o percurso.", "warn")
            return
        self._route_running = True
        self._route_index = 1
        self._route_cursor = self._route[0]
        self.move_button.configure(text="Parar percurso", fg_color=DANGER, hover_color=DANGER_HOVER)
        self.log(f"Percurso iniciado a {self.speed_value:.0f} km/h.", "ok")
        self._tick_route()

    def stop_route(self) -> None:
        if self._move_job is not None:
            self.after_cancel(self._move_job)
            self._move_job = None
        if self._route_running:
            self.log("Percurso interrompido.", "info")
        self._route_running = False
        self.move_button.configure(text="Iniciar percurso", fg_color="#21262d", hover_color="#30363d")

    def _tick_route(self) -> None:
        if not self._route_running or self._route_cursor is None:
            return
        step_m = self.speed_value / 3.6 * (MOVE_TICK_MS / 1000)
        target = self._route[self._route_index]
        remaining = step_m

        while remaining > 0:
            distance = geo.distance_m(self._route_cursor, target)
            if distance <= remaining:
                self._route_cursor = target
                remaining -= distance
                self._route_index += 1
                if self._route_index >= len(self._route):
                    self._set_selected(*self._route_cursor, describe=False)
                    self.worker.request_location(*self._route_cursor)
                    self.log("Fim do percurso.", "ok")
                    self.stop_route()
                    return
                target = self._route[self._route_index]
            else:
                self._route_cursor = geo.move_towards(self._route_cursor, target, remaining)
                remaining = 0

        self._set_selected(*self._route_cursor, describe=False)
        self.worker.request_location(*self._route_cursor)
        self._move_job = self.after(MOVE_TICK_MS, self._tick_route)

    # -------------------------------------------------------------------- polling
    def _poll(self) -> None:
        for event in self.worker.drain_events():
            self._handle_event(event)
        while True:
            try:
                message, level = self._async_logs.get_nowait()
            except queue.Empty:
                break
            self.log(message, level)
        while True:
            try:
                callback, (ok, value) = self._bg_results.get_nowait()
            except queue.Empty:
                break
            callback(ok, value)
        self.after(POLL_MS, self._poll)

    def _handle_event(self, event: device.Event) -> None:
        if event.kind == "log":
            self.log(event.payload.get("message", ""), event.payload.get("level", "info"))
        elif event.kind == "devices":
            self._update_devices(event.payload.get("devices", []))
        elif event.kind == "state":
            self._update_state(event.payload.get("state", ""), event.payload.get("info"))
        elif event.kind == "location":
            self._update_simulation(event.payload)
        elif event.kind == "tunnel_required":
            self._on_tunnel_required()

    def _update_devices(self, devices: list[dict[str, Any]]) -> None:
        self._devices = [
            {
                "udid": entry["udid"],
                "label": f"{entry.get('name', 'iPhone')} - iOS {entry.get('ios_version', '?')}",
            }
            for entry in devices
        ]
        if not self._devices:
            self.device_menu.configure(values=["Nenhum aparelho"])
            self.device_menu.set("Nenhum aparelho")
            return
        labels = [entry["label"] for entry in self._devices]
        self.device_menu.configure(values=labels)
        last = self.store.get("last_udid")
        chosen = next((e["label"] for e in self._devices if e["udid"] == last), labels[0])
        self.device_menu.set(chosen)
        self.log(f"{len(labels)} aparelho(s) encontrado(s).", "ok")

    def _update_state(self, state: str, info: device.DeviceInfo | None) -> None:
        self._state = state
        if state in (device.STATE_DISCONNECTED, device.STATE_ERROR):
            self._device_info = None
            self._simulating = False
        elif info is not None:
            self._device_info = info
        self._refresh_status()

    def _refresh_status(self) -> None:
        colors = {
            device.STATE_DISCONNECTED: ("#30363d", "  Desconectado  "),
            device.STATE_CONNECTING: (WARN, "  Conectando...  "),
            device.STATE_CONNECTED: (OK, "  Conectado  "),
            device.STATE_ERROR: (DANGER, "  Erro  "),
        }
        color, text = colors.get(self._state, ("#30363d", "  Desconectado  "))
        if self._state == device.STATE_CONNECTED and self._simulating:
            color, text = ACCENT, "  Simulando GPS  "
        self.status_pill.configure(fg_color=color, text=text)

        connected = self._state == device.STATE_CONNECTED
        self.connect_button.configure(
            text="Desconectar" if connected else "Conectar",
            fg_color=DANGER if connected else ACCENT,
            hover_color=DANGER_HOVER if connected else ACCENT_HOVER,
        )

        info = self._device_info
        if info is None:
            for key, label in self.info_labels.items():
                if key != "tunnel":  # o tunnel tem monitor proprio
                    label.configure(text="-")
            return
        mode = {True: "ativo", False: "desativado", None: "n/d"}[info.developer_mode]
        self.info_labels["name"].configure(text=info.name)
        self.info_labels["ios"].configure(text=info.ios_version)
        self.info_labels["mode"].configure(text=mode)
        self.info_labels["link"].configure(text="tunel RSD" if info.uses_tunnel else "lockdown/USB")

    def _update_simulation(self, payload: dict[str, Any]) -> None:
        self._simulating = bool(payload.get("active"))
        if self._simulating:
            latitude, longitude = payload["lat"], payload["lon"]
            self.pin.set_text("GPS ativo")
            self.map_hint.configure(
                text=f"Simulando {latitude:.5f}, {longitude:.5f} - o iPhone reporta este ponto"
            )
            if not self._route_running:
                self.log(f"Localizacao aplicada: {latitude:.6f}, {longitude:.6f}", "ok")
        else:
            self.pin.set_text("Alvo")
            self.map_hint.configure(text="Clique no mapa para escolher o ponto")
        self._refresh_status()

    # ---------------------------------------------------------------- encerramento
    def _on_close(self) -> None:
        self.stop_route()
        try:
            self.store.set("last_zoom", int(self.map_widget.zoom))
        except Exception:  # noqa: BLE001
            pass
        self.worker.submit("clear_location")
        self.worker.shutdown()
        self.worker.join(timeout=3)
        self.destroy()


def run() -> None:
    App().mainloop()
