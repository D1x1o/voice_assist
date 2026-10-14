"""
Голосовой ассистент (мини-Джарвис)
------------------------------------
Простой GUI на tkinter:
- Кнопка "Начать прослушивание" / "Остановить прослушивание" — включает
  непрерывный режим: приложение само слушает микрофон, само определяет,
  когда вы начали и закончили говорить (по паузам в речи), и после каждой
  фразы автоматически распознаёт её — нажимать ничего между фразами не нужно.
- Если сказанная фраза совпадает с одной из известных команд
  (см. список COMMANDS ниже) — сразу выполняется соответствующее действие
  (например, "сверни все окна")
- Если команда не распознана — текст просто вставляется в поле,
  как обычная диктовка

Распознавание речи выполняется офлайн через faster-whisper.

Установка зависимостей:
    pip install faster-whisper sounddevice numpy

Запуск:
    python voice_input_app.py

При первом запуске модель Whisper будет автоматически скачана
(один раз, дальше берётся из локального кэша).

Как добавить новую команду:
    Допишите строку в список COMMANDS — укажите список ключевых слов
    (все должны встретиться во фразе) и функцию, которая будет вызвана.
"""

import os
import sys

# На Windows библиотеки cuBLAS/cuDNN, установленные через pip
# (пакеты nvidia-cublas-cu12 и nvidia-cudnn-cu12), не подхватываются
# автоматически — Windows не использует LD_LIBRARY_PATH, как Linux.
# Поэтому явно регистрируем их папки с DLL, ДО импорта faster_whisper.
if sys.platform == "win32":
    try:
        import nvidia.cublas
        import nvidia.cudnn

        cublas_bin = os.path.join(nvidia.cublas.__path__[0], "bin")
        cudnn_bin = os.path.join(nvidia.cudnn.__path__[0], "bin")

        os.add_dll_directory(cublas_bin)
        os.add_dll_directory(cudnn_bin)

        # Некоторые версии ctranslate2 ищут DLL именно через PATH,
        # а не через список директорий из add_dll_directory —
        # поэтому дублируем и туда, на всякий случай.
        os.environ["PATH"] = (
            cublas_bin + os.pathsep + cudnn_bin + os.pathsep + os.environ.get("PATH", "")
        )
    except ImportError:
        # Пакеты nvidia-cublas-cu12 / nvidia-cudnn-cu12 не установлены —
        # см. requirements-gpu.txt.
        pass

import tkinter as tk
from tkinter import scrolledtext
import threading
import queue
import time
import numpy as np
import sounddevice as sd

# ---------- Настройки распознавания ----------
# Размер модели Whisper: "tiny", "base", "small", "medium", "large-v3"
# Чем больше модель — тем точнее распознавание, но дольше загрузка/обработка.
# "small" — хороший баланс скорости и качества для ru/en.
MODEL_SIZE = "small" # Конкретно для разработки буду использовать small версию для быстрой отладки
SAMPLE_RATE = 16000  # Whisper ожидает 16 кГц

# "cuda" — работа на видеокарте NVIDIA (нужны nvidia-cublas-cu12 и
#          nvidia-cudnn-cu12==9.*, см. requirements-gpu.txt).
# "cpu"  — работа на процессоре, без дополнительных зависимостей.
DEVICE = "cuda"
COMPUTE_TYPE = "float16" if DEVICE == "cuda" else "int8"

# ---------- Настройки непрерывного прослушивания (VAD) ----------
BLOCK_DURATION = 0.1          # секунд на один блок аудио от микрофона
SILENCE_DURATION = 0.8        # сколько секунд тишины считать концом фразы
MIN_SPEECH_DURATION = 0.3     # минимальная длина фразы, чтобы не ловить шум/щелчки
CALIBRATION_DURATION = 1.0    # секунд на замер уровня фонового шума при старте
NOISE_MARGIN_MULTIPLIER = 3.0 # во сколько раз громче шума должна быть речь
MIN_THRESHOLD = 0.01          # минимальный порог громкости (на случай очень тихого шума)

SILENCE_BLOCKS = max(1, int(SILENCE_DURATION / BLOCK_DURATION))
MIN_SPEECH_BLOCKS = max(1, int(MIN_SPEECH_DURATION / BLOCK_DURATION))


# ---------- Голосовые команды ----------
# Каждая команда — это (список ключевых слов, функция-действие, подпись).
# Команда срабатывает, если ВСЕ ключевые слова из списка встречаются
# в распознанном тексте (в любом порядке, без учёта регистра).
# Если ни одна команда не подошла — текст просто вставляется в поле,
# как обычная диктовка.

import ctypes
import webbrowser
import subprocess
import urllib.parse


def minimize_all_windows():
    """Свернуть/развернуть все окна (Win+D — переключатель "Показать рабочий стол")."""
    user32 = ctypes.windll.user32
    VK_LWIN, VK_D, KEYEVENTF_KEYUP = 0x5B, 0x44, 0x0002
    user32.keybd_event(VK_LWIN, 0, 0, 0)
    user32.keybd_event(VK_D, 0, 0, 0)
    user32.keybd_event(VK_D, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_LWIN, 0, KEYEVENTF_KEYUP, 0)


def lock_computer():
    """Заблокировать компьютер (Win+L)."""
    ctypes.windll.user32.LockWorkStation()


def open_browser():
    """Открыть браузер по умолчанию."""
    webbrowser.open("https://google.com")


def open_notepad():
    """Открыть Блокнот."""
    subprocess.Popen(["notepad.exe"])


def open_search(query):
    """Открыть браузер сразу с результатами поиска Google по запросу."""
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
    webbrowser.open(url)

def open_steam():
    """Открыть Стим."""
    subprocess.Popen(["C:\Program Files (x86)\Steam\steam.exe"])



# Фразы-триггеры для поиска в интернете. После триггера идёт сам запрос —
# он "захватывается" автоматически (см. match_command), а не задаётся заранее.
# Отсортированы по длине (длинные проверяются первыми), чтобы не обрезать
# запрос по более короткому совпадающему триггеру.
SEARCH_TRIGGERS = [
    "найди в интернете",
    "найди в интернет",
    "поищи в интернете",
    "поищи в интернет",
    "открой в интернет",
    "открой в интернете",
    "погугли",
    "загугли",
    "найди",
]
SEARCH_TRIGGERS.sort(key=len, reverse=True)


COMMANDS = [
    (["сверни", "окна"], minimize_all_windows, "Свернуть все окна"),
    (["сверни", "все"], minimize_all_windows, "Свернуть все окна"),
    (["разверни", "окна"], minimize_all_windows, "Показать окна"),
    (["покажи", "рабочий", "стол"], minimize_all_windows, "Показать рабочий стол"),
    (["заблокируй", "компьютер"], lock_computer, "Заблокировать компьютер"),
    (["открой", "браузер"], open_browser, "Открыть браузер"),
    (["открой", "блокнот"], open_notepad, "Открыть блокнот"),
    (["открой", "стим"], open_steam, "Открыть стим"),
]


def match_command(text):
    """Ищет подходящую команду в тексте. Возвращает (функция, подпись) или (None, None)."""
    text_lower = text.lower()

    # Сначала проверяем команды поиска — они "захватывают" весь текст после триггера
    for trigger in SEARCH_TRIGGERS:
        idx = text_lower.find(trigger)
        if idx != -1:
            query = text[idx + len(trigger):].strip(" ,.:;-—")
            if query:
                return (lambda q=query: open_search(q)), f'Поиск в интернете: "{query.capitalize()}"'

    # Затем — обычные команды по набору ключевых слов
    for keywords, action, label in COMMANDS:
        if all(keyword in text_lower for keyword in keywords):
            return action, label

    return None, None


class VoiceApp:
    def __init__(self, root):
        self.root = root
        root.title("Голосовой ассистент")
        width, height = 640, 480
        x = (root.winfo_screenwidth() - width) // 2
        y = (root.winfo_screenheight() - height) // 2
        root.geometry(f"{width}x{height}+{x}+{y}")
        root.minsize(480, 360)

        self.model = None
        self.is_listening = False
        self.stream = None
        self.silence_threshold = MIN_THRESHOLD

        # Состояние детектора речи (используется в audio-callback)
        self.speech_active = False
        self.speech_buffer = []
        self.silence_counter = 0

        # Очередь готовых фраз для распознавания (заполняется в callback,
        # разбирается фоновым потоком-воркером)
        self.audio_queue = queue.Queue()
        self.worker_thread = None

        # ---------- UI ----------
        self.status_var = tk.StringVar(value="Загрузка модели Whisper, подождите...")
        

        self.listen_button = tk.Button(
            root,
            text="🎤 Начать прослушивание",
            font=("Arial", 14),
            command=self.toggle_listening,
            state="disabled",
            bg="#4CAF50",
            fg="white",
            activebackground="#43A047",
            padx=20,
            pady=10,
        )
        self.listen_button.pack(pady=15)

        self.status_label = tk.Label(
            root, textvariable=self.status_var, font=("Arial", 10), fg="gray"
        )
        self.status_label.pack(pady=(0, 5))

        self.text_box = scrolledtext.ScrolledText(
            root, wrap=tk.WORD, font=("Arial", 12), height=15
        )
        self.text_box.pack(fill="both", expand=True, padx=10, pady=10)
        self.text_box.config(state="disabled")
        
        
        clear_button = tk.Button(root, text="Очистить текст", command=self.clear_text)
        clear_button.pack(pady=(0, 10))

        # Модель грузим в фоне, чтобы окно сразу открывалось
        threading.Thread(target=self.load_model, daemon=True).start()
        
        

    # ---------- Загрузка модели ----------
    def load_model(self):
        make_a_log(self,"Начало загрузки модели", "INFO")
        from faster_whisper import WhisperModel

        try:
            self.model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
        except Exception as e:
            error_message = (
                f"Не удалось загрузить модель на {DEVICE}: {e}\n"
                f"Проверьте, что установлены nvidia-cublas-cu12 и "
                f"nvidia-cudnn-cu12==9.*, либо смените DEVICE на \"cpu\" "
                f"в начале файла."
            )
            self.root.after(0, lambda: self._on_model_load_error(error_message))
            return

        self.root.after(0, self._on_model_loaded)

    def _on_model_load_error(self, message):
        self.status_var.set(message)

    def _on_model_loaded(self):
        self.status_var.set("Модель загружена. Нажмите кнопку, чтобы начать слушать.")
        make_a_log(self,"Голосовая модель загружена","INFO")
        self.listen_button.config(state="normal")

    # ---------- Включение/выключение прослушивания ----------
    def toggle_listening(self):
        if not self.is_listening:
            self.start_listening()
        else:
            self.stop_listening()

    def start_listening(self):
        self.listen_button.config(state="disabled")
        self.status_var.set("Калибровка микрофона (не говорите)...")
        make_a_log(self,"Колибровка микрофона","INFO")
        threading.Thread(target=self._calibrate_and_start, daemon=True).start()

    def _calibrate_and_start(self):
        # Замеряем уровень фонового шума, чтобы понять, где граница "тишина/речь"
        frames = []

        def calib_callback(indata, frames_count, time_info, status):
            frames.append(indata.copy())

        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=calib_callback
            ):
                sd.sleep(int(CALIBRATION_DURATION * 1000))
        except Exception as e:
            self.root.after(0, lambda: self._on_listen_error(f"Ошибка доступа к микрофону: {e}"))
            return

        if frames:
            noise = np.concatenate(frames, axis=0).flatten()
            noise_rms = float(np.sqrt(np.mean(noise ** 2)))
        else:
            noise_rms = 0.0

        self.silence_threshold = max(noise_rms * NOISE_MARGIN_MULTIPLIER, MIN_THRESHOLD)

        self.root.after(0, self._begin_listening_stream)

    def _begin_listening_stream(self):
        self.is_listening = True
        self.speech_active = False
        self.speech_buffer = []
        self.silence_counter = 0

        # Запускаем фоновый поток, который разбирает очередь фраз на распознавание
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()
        make_a_log(self,"Начало записи","INFO")

        block_size = int(SAMPLE_RATE * BLOCK_DURATION)
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=block_size,
            callback=self._audio_callback,
        )
        self.stream.start()

        self.listen_button.config(
            text="⏹ Остановить прослушивание", bg="#f44336", activebackground="#e53935", state="normal"
        )
        self.status_var.set("Слушаю... Говорите (в том числе команды).")

    def _on_listen_error(self, message):
        self.status_var.set(message)
        self.listen_button.config(state="normal")

    def stop_listening(self):
        self.is_listening = False
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None

        # Останавливаем воркер-поток
        self.audio_queue.put(None)

        self.listen_button.config(
            text="🎤 Начать прослушивание", bg="#4CAF50", activebackground="#43A047"
        )
        self.status_var.set("Прослушивание остановлено.")
        make_a_log(self,"Запись остановлена","INFO")

    # ---------- Аудио-callback (вызывается в отдельном потоке sounddevice) ----------
    def _audio_callback(self, indata, frames_count, time_info, status):
        volume = float(np.sqrt(np.mean(indata ** 2)))

        if volume > self.silence_threshold:
            # Идёт речь
            self.speech_buffer.append(indata.copy())
            self.silence_counter = 0
            self.speech_active = True
        elif self.speech_active:
            # Тишина, но до этого была речь — считаем, ждём конца фразы
            self.speech_buffer.append(indata.copy())
            self.silence_counter += 1
            if self.silence_counter >= SILENCE_BLOCKS:
                # Конец фразы
                if len(self.speech_buffer) >= MIN_SPEECH_BLOCKS:
                    audio_chunk = np.concatenate(self.speech_buffer, axis=0).flatten()
                    self.audio_queue.put(audio_chunk)
                self.speech_buffer = []
                self.speech_active = False
                self.silence_counter = 0
        # иначе: тишина и до этого тоже была тишина — ничего не делаем

    # ---------- Фоновый воркер: разбирает очередь фраз ----------
    def _worker_loop(self):
        while True:
            audio_chunk = self.audio_queue.get()
            if audio_chunk is None:
                break
            self.root.after(0, lambda: self.status_var.set("Распознаю фразу..."))
            make_a_log(self,"Распознование","INFO")
            self._process_chunk(audio_chunk)

    def _process_chunk(self, audio_chunk):
        try:
            segments, info = self.model.transcribe(audio_chunk, language=None, beam_size=5)
            text = " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as e:
            error_message = f"Ошибка распознавания: {e}"
            self.root.after(0, lambda: self._on_listen_error(error_message))
            return

        self.root.after(0, lambda: self._on_phrase_recognized(text, info.language))

    def _on_phrase_recognized(self, text, language):
        if not text:
            if self.is_listening:
                self.status_var.set("Слушаю...")
                make_a_log(self,"Начало записи","INFO")  
            return

        action, label = match_command(text)

        if action is not None:
            try:
                action()
                self.status_var.set(f'Выполнено: ' + label)                
            except Exception as e:
                make_a_log(self,f'Ошибка при выполнении команды "{label}": {e}',"ERROR")  
                self.status_var.set(f'Ошибка при выполнении команды "{label}": {e}')
            make_a_log(self,f"[Команда: {label}]","INFO")            
            self.text_box.see(tk.END)
        else:      
            make_a_log(self,f"Язык {language.upper()} : {text}","INPUT")
            self.text_box.see(tk.END)
            if self.is_listening:
                self.status_var.set(f"Распознано (язык: {language.upper()}). Слушаю дальше...")
        

    def clear_text(self):
        self.text_box.config(state="normal")
        self.text_box.delete("1.0", tk.END)
        self.text_box.config(state="disabled")

def make_a_log(self, log_text, log_type):
    self.text_box.config(state="normal")
    colors = {
                    "green": "#16803D",
                    "yellow": "#D4A000",
                    "red": "#DC2626",
                    "gray": "#808080"
                }
    for color_name, hex_color in colors.items():
        self.text_box.tag_configure(color_name, foreground=hex_color)
    match (log_type):
        case "INFO":
            self.text_box.insert(tk.END, "--- INFO ","green")
            self.text_box.insert(tk.END, f" {log_text}\n")
            self.text_box.config(state="disabled")
            return
        case "ERROR":
                self.text_box.insert(tk.END, "--- ERROR ","red")
                self.text_box.insert(tk.END, f"{log_text}\n")
                self.text_box.config(state="disabled")
                return
        case "INPUT":
                self.text_box.insert(tk.END, "--- INPUT ","gray")
                self.text_box.insert(tk.END, f"{log_text}\n")
                self.text_box.config(state="disabled")
                return
if __name__ == "__main__":
    root = tk.Tk()
    app = VoiceApp(root)
    root.mainloop()