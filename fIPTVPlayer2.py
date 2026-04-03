import sys
import requests
import json
import os

from PySide6.QtCore import QUrl, Qt, QSize, QThread, Signal, QTimer
from PySide6.QtGui import QIcon, QColor
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFrame, 
    QStackedWidget, QListWidgetItem, QFormLayout, QSpacerItem, QSizePolicy
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget

# Importy z biblioteki PySide6-Fluent-Widgets
from qfluentwidgets import (
    NavigationWidget, FluentWindow, SubtitleLabel, 
    BodyLabel, LineEdit, PrimaryPushButton, PushButton, ListWidget, 
    Slider, ProgressBar, TransparentToolButton, FluentIcon as FIF,
    setTheme, Theme, PasswordLineEdit, ComboBox, CaptionLabel,
    CardWidget, MessageBox, NavigationItemPosition, TitleLabel, 
    SingleDirectionScrollArea, InfoBar, InfoBarPosition, setThemeColor
)

CONFIG_FILE = "iptv_config_fluent.json"

TR = {
    "saved": "Zapisane", "add": "DODAJ", "settings": "Opcje",
    "channels": "Kanały", "url": "URL", "host": "Host", "user": "Użytkownik", "pass": "Hasło",
    "mac": "MAC", "connect": "Połącz", "save": "Zapisz", "delete_list": "Usuń", 
    "ready": "Gotowy", "loading": "Wczytywanie...", "play": "Odtwarzam:",
    "theme_label": "Motyw:", "theme_dark": "Ciemny", "theme_light": "Jasny",
    "placeholder": "fIPTV Player\nWybierz kanał z listy", "error": "Błąd", "custom_name": "Własna nazwa"
}

# ==========================================
# WORKERY DO WIELOWĄTKOWOŚCI (QThread)
# ==========================================

class M3UWorker(QThread):
    finished = Signal(list, list)
    error = Signal(str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            r = requests.get(self.url, stream=True, timeout=15)
            r.raise_for_status()

            names = []
            data = []
            name = "Kanał"

            for line_bytes in r.iter_lines():
                if not line_bytes: continue
                line = line_bytes.decode('utf-8', errors='ignore').strip()

                if line.startswith("#EXTINF"):
                    idx = line.rfind(',')
                    if idx != -1:
                        name = line[idx+1:].strip()
                elif line.startswith("http"):
                    names.append(name)
                    data.append({"url": line, "type": "direct"})
                    name = "Kanał"

            self.finished.emit(names, data)
        except Exception as e:
            self.error.emit(f"Błąd sieci/parsoawnia: {str(e)}")


class XtreamWorker(QThread):
    finished = Signal(list, list)
    error = Signal(str)

    def __init__(self, host, user, password):
        super().__init__()
        self.host = host
        self.user = user
        self.password = password

    def run(self):
        try:
            url = f"{self.host}/player_api.php?username={self.user}&password={self.password}&action=get_live_streams"
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            
            names = []
            data = []
            
            for s in r.json():
                names.append(s.get('name', 'Stream'))
                data.append({
                    "url": f"{self.host}/live/{self.user}/{self.password}/{s.get('stream_id')}.ts", 
                    "type": "direct"
                })
                
            self.finished.emit(names, data)
        except Exception as e:
            self.error.emit(str(e))


class StalkerWorker(QThread):
    finished = Signal(list, list, str, object)
    error = Signal(str)

    def __init__(self, portal, mac):
        super().__init__()
        self.portal = portal
        self.mac = mac

    def run(self):
        try:
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (MAG250) AppleWebKit/533.3',
                'X-User-Agent': 'Model: MAG250'
            })
            session.cookies.set('mac', self.mac)
            
            r = session.get(f"{self.portal}/server/load.php?type=stb&action=handshake", timeout=15)
            token = r.json().get('js', {}).get('token')
            
            if not token: 
                r = session.get(f"{self.portal}/server/load.php?type=stb&action=get_profile", timeout=15)
                token = r.cookies.get('token')
                
            if not token: 
                raise Exception("Błąd autoryzacji MAC (brak tokenu)")
                
            session.headers.update({'Authorization': f'Bearer {token}'})
            r = session.get(f"{self.portal}/server/load.php?type=itv&action=get_all_channels", timeout=15)
            chans = r.json().get('js', {}).get('data', [])
            
            names = []
            data = []
            for c in chans:
                names.append(c.get('name', 'Stalker Ch'))
                data.append({"cmd": c.get('cmd', ''), "type": "stalker"})
                
            self.finished.emit(names, data, self.portal, session)
        except Exception as e:
            self.error.emit(str(e))

# ==========================================
# GŁÓWNA KLASA APLIKACJI
# ==========================================

class IPTVPlayer(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("fIPTV Player")
        self.setMinimumSize(900, 650)
        
        # 1. Najpierw wczytaj konfigurację i motyw
        self.saved_lists = []
        self.current_theme = "Ciemny"
        self.load_config() 
        
        self.navigationInterface.setExpandWidth(200)
        
        # Zmienne do ładowania w tle
        self.worker = None
        self.channels_data = []
        self.current_portal_url = None
        self.stalker_session = None
        
        # Zmienne dla Batch Loadingu
        self._pending_names = []
        self._pending_data = []
        self._batch_size = 1000 
        self._batch_timer = QTimer(self)
        self._batch_timer.timeout.connect(self._process_list_batch)

        # Silnik wideo
        self.media_player = QMediaPlayer()
        self.video_widget = QVideoWidget()
        self.audio_output = QAudioOutput()
        self.media_player.setVideoOutput(self.video_widget)
        self.media_player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.7)

        # Inicjalizacja interfejsu
        self.init_pages()
        self.update_ui_texts()
        
        self.media_player.errorOccurred.connect(self.handle_player_error)

    def init_pages(self):
        self.player_page = QWidget(); self.player_page.setObjectName("player_page")
        self.saved_page = QWidget(); self.saved_page.setObjectName("saved_page")
        self.add_page = QWidget(); self.add_page.setObjectName("add_page")
        self.settings_page = QWidget(); self.settings_page.setObjectName("settings_page")

        self.setup_player_page()
        self.setup_add_page()
        self.setup_saved_page()
        self.setup_settings_page()

        self.addSubInterface(self.player_page, FIF.VIDEO, "Odtwarzacz")
        self.addSubInterface(self.saved_page, FIF.HEART, "Zapisane")
        self.navigationInterface.addSeparator()
        self.addSubInterface(self.add_page, FIF.ADD, "Import")
        self.addSubInterface(self.settings_page, FIF.SETTING, "Opcje", NavigationItemPosition.BOTTOM)

    def setup_player_page(self):
        layout = QHBoxLayout(self.player_page)
        layout.setContentsMargins(10, 40, 10, 10)
        layout.setSpacing(10)
        
        channel_panel = QFrame()
        channel_layout = QVBoxLayout(channel_panel)
        channel_layout.setContentsMargins(0, 0, 0, 0)
        self.chan_title = SubtitleLabel(TR["channels"])
        self.channel_list = ListWidget()
        
        self.channel_list.setUniformItemSizes(True) 
        self.channel_list.itemDoubleClicked.connect(self.play_selected_channel)
        
        channel_layout.addWidget(self.chan_title)
        channel_layout.addWidget(self.channel_list)
        
        video_panel = QFrame()
        video_layout = QVBoxLayout(video_panel)
        video_layout.setContentsMargins(0, 0, 0, 0)
        
        self.video_stack = QStackedWidget()
        self.placeholder_label = BodyLabel(TR["placeholder"])
        self.placeholder_label.setAlignment(Qt.AlignCenter)
        self.video_stack.addWidget(self.placeholder_label)
        self.video_stack.addWidget(self.video_widget)
        
        controls_card = CardWidget()
        controls_layout = QHBoxLayout(controls_card)
        controls_layout.setContentsMargins(10, 5, 10, 5)
        
        self.btn_play = TransparentToolButton(FIF.PLAY, controls_card)
        self.btn_pause = TransparentToolButton(FIF.PAUSE, controls_card)
        self.btn_stop = TransparentToolButton(FIF.CLOSE, controls_card)
        self.btn_play.clicked.connect(self.media_player.play)
        self.btn_pause.clicked.connect(self.media_player.pause)
        self.btn_stop.clicked.connect(self.media_player.stop)
        
        self.status_label = CaptionLabel(TR["ready"])
        self.status_label.setWordWrap(True)
        
        self.vol_slider = Slider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(70)
        self.vol_slider.setMinimumWidth(80)
        self.vol_slider.setMaximumWidth(150)
        self.vol_slider.valueChanged.connect(lambda v: self.audio_output.setVolume(v/100))

        controls_layout.addWidget(self.btn_play)
        controls_layout.addWidget(self.btn_pause)
        controls_layout.addWidget(self.btn_stop)
        controls_layout.addWidget(self.status_label, 1)
        controls_layout.addWidget(self.vol_slider)

        self.progress_bar = ProgressBar()
        self.progress_bar.hide()

        video_layout.addWidget(self.video_stack, 1)
        video_layout.addWidget(self.progress_bar)
        video_layout.addWidget(controls_card)

        layout.addWidget(channel_panel, 1)
        layout.addWidget(video_panel, 3)

    def _create_fluent_button(self, text, callback, is_primary=True):
        """Tworzy przycisk zgodny ze stylem Fluent bez wymuszania CSS"""
        btn = PrimaryPushButton(text) if is_primary else PushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(callback)
        return btn

    def setup_add_page(self):
        outer_layout = QVBoxLayout(self.add_page)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        
        scroll_area = SingleDirectionScrollArea(self.add_page)
        scroll_area.setWidgetResizable(True)
        scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        
        container = QWidget()
        container.setObjectName("importContainer")
        container.setStyleSheet("#importContainer { background: transparent; }")
        
        layout = QVBoxLayout(container)
        layout.setContentsMargins(40, 20, 40, 40); layout.setSpacing(30)

        self.m3u_name = LineEdit(); self.m3u_name.setPlaceholderText("Nazwa (opcjonalnie)")
        self.m3u_input = LineEdit(); self.m3u_input.setPlaceholderText("http://...")
        
        self.xc_name = LineEdit(); self.xc_name.setPlaceholderText("Nazwa (opcjonalnie)")
        self.xc_host = LineEdit(); self.xc_host.setPlaceholderText("Host (np. http://host:port)")
        self.xc_user = LineEdit(); self.xc_user.setPlaceholderText("Użytkownik")
        self.xc_pass = PasswordLineEdit(); self.xc_pass.setPlaceholderText("Hasło")
        
        self.st_name = LineEdit(); self.st_name.setPlaceholderText("Nazwa (opcjonalnie)")
        self.st_host = LineEdit(); self.st_host.setPlaceholderText("Portal URL")
        self.st_mac = LineEdit(); self.st_mac.setPlaceholderText("MAC (00:1A:79:XX:XX:XX)")

        def create_section(title, widgets, conn_cb, save_cb):
            sec = QVBoxLayout(); sec.setSpacing(10)
            t = TitleLabel(title)
            sec.addWidget(t)
            for w in widgets: sec.addWidget(w)
            
            btn_row = QHBoxLayout()
            btn_row.setSpacing(10)
            btn_row.addWidget(self._create_fluent_button("Połącz", conn_cb, True), 1)
            btn_row.addWidget(self._create_fluent_button("Zapisz", save_cb, False), 1)
            btn_row.addStretch(2)
            
            sec.addLayout(btn_row)
            return sec

        layout.addLayout(create_section("Import M3U", [self.m3u_name, self.m3u_input], self.load_m3u, self.save_m3u_only))
        layout.addWidget(QFrame(frameShape=QFrame.HLine))
        layout.addLayout(create_section("Xtream Codes API", [self.xc_name, self.xc_host, self.xc_user, self.xc_pass], self.load_xtream, self.save_xtream_only))
        layout.addWidget(QFrame(frameShape=QFrame.HLine))
        layout.addLayout(create_section("Stalker Portal", [self.st_name, self.st_host, self.st_mac], self.load_stalker, self.save_stalker_only))
        layout.addStretch(1)

        scroll_area.setWidget(container)
        outer_layout.addWidget(scroll_area)

    def save_m3u_only(self):
        url = self.m3u_input.text().strip()
        name = self.m3u_name.text().strip()
        if url:
            self.save_config({"type": "m3u", "url": url, "name": name})
            InfoBar.success("Zapisano", f"Lista '{name or url}' została dodana.", duration=2000, parent=self)

    def save_xtream_only(self):
        h, u, p = self.xc_host.text().strip(), self.xc_user.text().strip(), self.xc_pass.text().strip()
        name = self.xc_name.text().strip()
        if h and u and p:
            self.save_config({"type": "xtream", "host": h, "user": u, "pass": p, "name": name})
            InfoBar.success("Zapisano", f"Konto '{name or h}' zostało dodane.", duration=2000, parent=self)

    def save_stalker_only(self):
        h, m = self.st_host.text().strip(), self.st_mac.text().strip()
        name = self.st_name.text().strip()
        if h and m:
            self.save_config({"type": "stalker", "url": h, "mac": m, "name": name})
            InfoBar.success("Zapisano", f"Portal '{name or m}' został dodany.", duration=2000, parent=self)

    def setup_saved_page(self):
        self.saved_page.setContentsMargins(30, 50, 30, 30)
        layout = QVBoxLayout(self.saved_page)
        layout.setSpacing(20)
        
        header = QHBoxLayout()
        header.addWidget(SubtitleLabel("Zapisane Listy Kanałów"))
        layout.addLayout(header)

        main_content = QHBoxLayout()
        main_content.setSpacing(20)
        
        self.saved_list_widget = ListWidget()
        self.saved_list_widget.setMinimumWidth(320)
        self.saved_list_widget.itemClicked.connect(self.display_item_for_edit)
        main_content.addWidget(self.saved_list_widget, 2)
        
        self.edit_panel = CardWidget()
        self.edit_layout = QVBoxLayout(self.edit_panel)
        self.edit_layout.setContentsMargins(25, 25, 25, 25)
        self.edit_layout.setSpacing(20)
        
        self.edit_title = TitleLabel("Edytuj dane")
        
        self.edit_form = QFormLayout()
        self.edit_form.setSpacing(15)
        self.edit_inputs = {}
        
        self.edit_layout.addWidget(self.edit_title)
        self.edit_layout.addLayout(self.edit_form)
        
        self.buttons_container = QVBoxLayout()
        self.buttons_container.setSpacing(10)
        
        row1 = QHBoxLayout()
        row1.setSpacing(10)
        self.btn_load_now = self._create_fluent_button("Załaduj listę", self.load_current_edit, True)
        self.btn_save_edit = self._create_fluent_button("Zaktualizuj", self.save_current_edit, False)
        row1.addWidget(self.btn_load_now)
        row1.addWidget(self.btn_save_edit)
        
        row2 = QHBoxLayout()
        # Przycisk usuwania w Fluent Design często jest zwykłym przyciskiem lub ma kolor akcentu
        self.btn_del_saved = self._create_fluent_button("Usuń trwale", self.delete_saved_list, False)
        row2.addWidget(self.btn_del_saved)
        
        self.buttons_container.addLayout(row1)
        self.buttons_container.addLayout(row2)
        
        self.edit_layout.addLayout(self.buttons_container)
        self.edit_layout.addStretch(1)
        
        self.edit_panel.hide()
        main_content.addWidget(self.edit_panel, 3)
        layout.addLayout(main_content)

    def display_item_for_edit(self, item):
        idx = item.data(Qt.UserRole)
        data = self.saved_lists[idx]
        self.current_edit_idx = idx
        
        while self.edit_form.count():
            child = self.edit_form.takeAt(0)
            if child.widget(): child.widget().deleteLater()
        
        self.edit_inputs = {}
        
        name_edit = LineEdit()
        name_edit.setText(data.get('name', ''))
        self.edit_form.addRow("Nazwa:", name_edit)
        self.edit_inputs['name'] = name_edit

        fields = []
        if data['type'] == 'm3u': fields = [('url', 'URL M3U:')]
        elif data['type'] == 'xtream': fields = [('host', 'Host API:'), ('user', 'Użytkownik:'), ('pass', 'Hasło:')]
        elif data['type'] == 'stalker': fields = [('url', 'Portal URL:'), ('mac', 'Adres MAC:')]
        
        for key, label in fields:
            edit = LineEdit()
            edit.setText(data.get(key, ''))
            self.edit_form.addRow(label, edit)
            self.edit_inputs[key] = edit
            
        self.edit_panel.show()

    def save_current_edit(self):
        if not hasattr(self, 'current_edit_idx'): return
        idx = self.current_edit_idx
        for key, edit in self.edit_inputs.items():
            self.saved_lists[idx][key] = edit.text().strip()
        self.save_config()
        InfoBar.success("Zaktualizowano", "Dane zostały pomyślnie zmienione.", duration=2000, parent=self)

    def load_current_edit(self):
        if not hasattr(self, 'current_edit_idx'): return
        item = self.saved_list_widget.item(self.current_edit_idx)
        self.load_from_saved(item)

    def delete_saved_list(self):
        if not hasattr(self, 'current_edit_idx'): return
        self.saved_lists.pop(self.current_edit_idx)
        self.edit_panel.hide()
        self.save_config()
        InfoBar.warning("Usunięto", "Lista została usunięta z pamięci.", duration=2000, parent=self)

    # ==========================================
    # LOGIKA ŁADOWANIA
    # ==========================================

    def _prepare_for_loading(self, message=TR["loading"]):
        self.set_loading(True, message)
        self._batch_timer.stop()
        self.channel_list.clear()
        self.channels_data.clear() 
        self._pending_names.clear()
        self._pending_data.clear()

    def _start_batch_loading(self, names, data, success_msg=""):
        self._pending_names = names
        self._pending_data = data
        self.channels_data = [] 
        
        self.status_label.setText(f"Przetwarzanie interfejsu (0/{len(names)})")
        self.progress_bar.setRange(0, len(names))
        self.switchTo(self.player_page)
        self._batch_timer.start(5)

    def _process_list_batch(self):
        if not self._pending_names:
            self._batch_timer.stop()
            self.set_loading(False, f"Załadowano {len(self.channels_data)} kanałów")
            self.progress_bar.setRange(0, 0)
            return

        chunk_names = self._pending_names[:self._batch_size]
        chunk_data = self._pending_data[:self._batch_size]
        
        self._pending_names = self._pending_names[self._batch_size:]
        self._pending_data = self._pending_data[self._batch_size:]

        self.channel_list.addItems(chunk_names)
        self.channels_data.extend(chunk_data)
        
        current_loaded = len(self.channels_data)
        total = current_loaded + len(self._pending_names)
        self.progress_bar.setValue(current_loaded)
        self.status_label.setText(f"Dodawanie elementów... ({current_loaded}/{total})")

    def _on_worker_error(self, err_msg):
        self.set_loading(False, f"Błąd: {err_msg}")

    def load_m3u(self):
        url = self.m3u_input.text().strip()
        if not url: return
        self._prepare_for_loading()
        self.worker = M3UWorker(url)
        self.worker.finished.connect(self._start_batch_loading)
        self.worker.error.connect(self._on_worker_error)
        self.worker.start()

    def load_xtream(self):
        h = self.xc_host.text().strip().rstrip('/')
        u = self.xc_user.text().strip()
        p = self.xc_pass.text().strip()
        if not all([h, u, p]): return
        self._prepare_for_loading()
        self.worker = XtreamWorker(h, u, p)
        self.worker.finished.connect(self._start_batch_loading)
        self.worker.error.connect(self._on_worker_error)
        self.worker.start()

    def load_stalker(self):
        p = self.st_host.text().strip().rstrip('/')
        m = self.st_mac.text().strip()
        if not p or not m: return
        self._prepare_for_loading()
        self.worker = StalkerWorker(p, m)
        self.worker.finished.connect(self._on_stalker_finished)
        self.worker.error.connect(self._on_worker_error)
        self.worker.start()

    def _on_stalker_finished(self, names, data, portal, session):
        self.current_portal_url = portal
        self.stalker_session = session
        self._start_batch_loading(names, data)

    def play_selected_channel(self, item):
        idx = self.channel_list.row(item)
        if idx < 0 or idx >= len(self.channels_data): return
        d = self.channels_data[idx]
        url = d.get("url", "")
        if d["type"] == "stalker":
            try:
                if not self.stalker_session: raise Exception("Brak sesji Stalker")
                cmd = d["cmd"].replace('ffrt ', '').strip()
                r = self.stalker_session.get(f"{self.current_portal_url}/server/load.php?type=itv&action=create_link&cmd={cmd}", timeout=10)
                url_raw = r.json().get('js', {}).get('cmd', '')
                url = url_raw.split()[-1] if ' ' in url_raw else url_raw
            except Exception as e: 
                self.status_label.setText(f"Błąd generowania linku: {e}")
                return
        if url:
            self.media_player.stop()
            self.video_stack.setCurrentIndex(1)
            self.status_label.setText(f"Gra: {item.text()}")
            self.media_player.setSource(QUrl(url))
            self.media_player.play()

    def handle_player_error(self, error, error_string):
        self.status_label.setText(f"Błąd strumienia: {error_string}")
        self.video_stack.setCurrentIndex(0)

    def set_loading(self, active, msg=""):
        self.status_label.setText(msg if msg else TR["loading"])
        self.progress_bar.setVisible(active)
        if active: 
            self.progress_bar.setRange(0, 0)
        QApplication.processEvents()

    def update_ui_texts(self):
        self.chan_title.setText(TR["channels"])
        self.placeholder_label.setText(TR["placeholder"])
        self.status_label.setText(TR["ready"])

    def change_theme(self, theme_text):
        if theme_text in ["Ciemny", "Dark"]:
            setTheme(Theme.DARK)
            self.current_theme = "Ciemny"
        else:
            setTheme(Theme.LIGHT)
            self.current_theme = "Jasny"
        self.save_config()

    def save_config(self, new_source=None):
        if new_source:
            exists = False
            for s in self.saved_lists:
                if s['type'] == new_source['type']:
                    if s['type'] == 'm3u' and s.get('url') == new_source.get('url'): exists = True; break
                    if s['type'] == 'xtream' and s.get('host') == new_source.get('host'): exists = True; break
                    if s['type'] == 'stalker' and s.get('mac') == new_source.get('mac'): exists = True; break
            if not exists: self.saved_lists.append(new_source)
        
        cfg = {"saved_lists": self.saved_lists, "theme": self.current_theme}
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
        self.refresh_saved_list_ui()

    def load_config(self):
        """Wczytuje konfigurację i natychmiast aplikuje motyw"""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    self.saved_lists = cfg.get("saved_lists", [])
                    theme_val = cfg.get("theme", "Ciemny")
                    self.current_theme = theme_val
                    # Aplikacja motywu przy starcie
                    if theme_val == "Jasny":
                        setTheme(Theme.LIGHT)
                    else:
                        setTheme(Theme.DARK)
            except: 
                setTheme(Theme.DARK)
        else:
            setTheme(Theme.DARK)

    def refresh_saved_list_ui(self):
        self.saved_list_widget.clear()
        for i, item in enumerate(self.saved_lists):
            name = item.get('name') or item.get('url', item.get('host', item.get('mac', 'Źródło')))
            li = QListWidgetItem(f"{item['type'].upper()} | {name}")
            li.setData(Qt.UserRole, i)
            self.saved_list_widget.addItem(li)

    def load_from_saved(self, item):
        data = self.saved_lists[item.data(Qt.UserRole)]
        if data['type'] == 'm3u':
            self.m3u_input.setText(data['url']); self.load_m3u()
        elif data['type'] == 'xtream':
            self.xc_host.setText(data['host']); self.xc_user.setText(data['user']); self.xc_pass.setText(data['pass']); self.load_xtream()
        elif data['type'] == 'stalker':
            self.st_host.setText(data['url']); self.st_mac.setText(data['mac']); self.load_stalker()

    def setup_settings_page(self):
        self.settings_page.setContentsMargins(20, 50, 20, 20)
        layout = QVBoxLayout(self.settings_page)
        form = QFormLayout()
        
        self.theme_combo = ComboBox()
        self.theme_combo.addItems(["Ciemny", "Jasny"])
        
        idx = self.theme_combo.findText(self.current_theme)
        if idx >= 0:
            self.theme_combo.setCurrentIndex(idx)
        
        self.theme_combo.currentTextChanged.connect(self.change_theme)
        
        form.addRow("Motyw:", self.theme_combo)
        
        layout.addWidget(SubtitleLabel("Ustawienia"))
        layout.addLayout(form)
        
        layout.addSpacing(30)
        layout.addWidget(SubtitleLabel("O twórcy"))
        
        about_card = CardWidget()
        about_layout = QVBoxLayout(about_card)
        about_layout.setContentsMargins(20, 20, 20, 20)
        about_layout.setSpacing(10)
        
        creator_label = BodyLabel("<b>Twórca:</b> dawid9707")
        github_label = BodyLabel('<b>GitHub:</b> <a href="https://github.com/dawid9707" style="color: palette(highlight); text-decoration: none;">https://github.com/dawid9707</a>')
        github_label.setOpenExternalLinks(True)
        
        about_layout.addWidget(creator_label)
        about_layout.addWidget(github_label)
        layout.addWidget(about_card)
        
        layout.addStretch(1)

    def closeEvent(self, event):
        self.media_player.stop()
        if self.worker and self.worker.isRunning():
            self.worker.quit()
            self.worker.wait(1000)
        super().closeEvent(event)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    # Motyw jest teraz ustawiany wewnątrz klasy przez load_config()
    w = IPTVPlayer()
    w.show()
    sys.exit(app.exec())
