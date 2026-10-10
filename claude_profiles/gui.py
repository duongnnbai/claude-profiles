"""The window: one card per profile, drawn on Tk canvases so it looks the same on Windows and macOS.

Slow work (PowerShell queries, starting processes, opening folders) runs on a worker
thread; results come back to the Tk thread through a queue.
"""
from __future__ import annotations

import base64
import queue
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk

from . import APP_TITLE, core, icons

IS_WIN = sys.platform == 'win32'

BG = '#F3F2EF'
HEADER_BG = '#FAFAF8'
LINE = '#E4E2DD'
CARD_BG = '#FFFFFF'
CARD_BORDER = '#E6E4DF'
TEXT = '#1F1F1E'
MUTED = '#8A8780'
GHOST_FG, GHOST_BG, GHOST_BORDER = '#3D3D3A', '#ECEBE7', '#E0DED8'
DOT_RUNNING, DOT_STOPPED, DOT_BUSY = '#22A06B', '#C9C6BF', '#E0A100'
BANNER_BG, BANNER_FG = '#FFF4DB', '#6B4E00'
CARD_W, CARD_H, GAP = 196, 222, 10


def blend(fg: str, bg: str, a: float) -> str:
    f, b = icons.hex_to_rgb(fg), icons.hex_to_rgb(bg)
    return '#' + ''.join(f'{round((x * a + y * (1 - a)) * 255):02X}' for x, y in zip(f, b))


def elide(font, text: str, width: int) -> str:
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + '…') > width:
        text = text[:-1]
    return text + '…'


class Pill:
    """A rounded button drawn on a canvas (Tk buttons ignore colors on macOS)."""

    def __init__(self, app, canvas, x, y, w, h, text, fill, fg, behind, command, border=None, font=None):
        self.app, self.canvas, self.w, self.h = app, canvas, w, h
        self.fill, self.fg, self.behind, self.border, self.command = fill, fg, behind, border, command
        self.enabled, self.hover, self.pressed = True, False, False
        self.tag = f'pill{id(self)}'
        self.img = canvas.create_image(x, y, anchor='nw', tags=self.tag)
        self.txt = canvas.create_text(x + w / 2, y + h / 2, text=text, font=font or app.f_button, tags=self.tag)
        canvas.tag_bind(self.tag, '<Enter>', self._enter)
        canvas.tag_bind(self.tag, '<Leave>', self._leave)
        canvas.tag_bind(self.tag, '<ButtonPress-1>', self._press)
        canvas.tag_bind(self.tag, '<ButtonRelease-1>', self._release)
        self._draw()

    def _draw(self):
        alpha = 0.5 if not self.enabled else 0.75 if self.pressed else 0.88 if self.hover else 1.0
        self.canvas.itemconfigure(self.img, image=self.app.pill_image(self.w, self.h, self.fill, self.border, alpha))
        fg = self.fg if self.enabled else blend(self.fg, blend(self.fill, self.behind, 0.5), 0.6)
        self.canvas.itemconfigure(self.txt, fill=fg)

    def set(self, text=None, enabled=None):
        if text is not None:
            self.canvas.itemconfigure(self.txt, text=text)
        if enabled is not None and enabled != self.enabled:
            self.enabled = enabled
            self._draw()

    def _enter(self, _e):
        self.hover = True
        self.canvas.configure(cursor='hand2' if self.enabled else '')
        self._draw()

    def _leave(self, _e):
        self.hover = self.pressed = False
        self.canvas.configure(cursor='')
        self._draw()

    def _press(self, _e):
        self.pressed = True
        self._draw()

    def _release(self, _e):
        fire = self.pressed and self.hover and self.enabled
        self.pressed = False
        self._draw()
        if fire:
            self.command()


def pill_widget(app, parent, text, bg, command, padx=16, height=32, font=None):
    """A standalone ghost-style pill button."""
    font = font or app.f_button
    w, h = font.measure(text) + app.px(padx) * 2, app.px(height)
    c = tk.Canvas(parent, width=w, height=h, bg=bg, highlightthickness=0, bd=0)
    Pill(app, c, 0, 0, w, h, text, GHOST_BG, GHOST_FG, bg, command, border=GHOST_BORDER, font=font)
    return c


class Card:
    def __init__(self, app, parent, profile):
        self.app, self.profile = app, profile
        px = app.px
        m, w, h = px(GAP), px(CARD_W), px(CARD_H)
        self.canvas = c = tk.Canvas(parent, width=w + 2 * m, height=h + 2 * m, bg=BG, highlightthickness=0, bd=0)
        c.create_image(0, 0, anchor='nw', image=app.card_image())
        self.cx = cx = m + w / 2
        c.create_image(cx, m + px(24), anchor='n', image=app.icon_image(profile.color, 58))
        c.create_text(cx, m + px(94), anchor='n', fill=TEXT, font=app.f_title,
                      text=elide(app.f_title, profile.name, w - px(32)))
        c.create_text(cx, m + px(116), anchor='n', fill=MUTED, font=app.f_small,
                      text='your main install' if profile.is_default else 'isolated profile')
        self.state_y = m + px(147)
        self.dot = c.create_image(cx, self.state_y, anchor='w')
        self.state = c.create_text(cx, self.state_y, anchor='w', fill=MUTED, font=app.f_small)
        self.button = Pill(app, c, m + px(16), m + px(166), w - px(32), px(30), 'Launch',
                           core.COLORS[profile.color], '#FFFFFF', CARD_BG, lambda: app.launch(profile))
        self.menu = c.create_text(m + w - px(21), m + px(19), text='⋯', fill='#9A978F', font=app.f_menu)
        c.tag_bind(self.menu, '<Enter>', lambda e: (c.itemconfigure(self.menu, fill=TEXT), c.configure(cursor='hand2')))
        c.tag_bind(self.menu, '<Leave>', lambda e: (c.itemconfigure(self.menu, fill='#9A978F'), c.configure(cursor='')))
        c.tag_bind(self.menu, '<ButtonRelease-1>', lambda e: app.show_menu(profile, e))
        c.bind('<Button-3>' if IS_WIN else '<Button-2>', lambda e: app.show_menu(profile, e))
        self._shown = None

    def show(self, dot_color, state, button, enabled):
        if self._shown == (dot_color, state, button, enabled):
            return
        self._shown = (dot_color, state, button, enabled)
        app, c = self.app, self.canvas
        dot_w, gap = app.px(7), app.px(6)
        left = self.cx - (dot_w + gap + app.f_small.measure(state)) / 2
        c.itemconfigure(self.dot, image=app.dot_image(dot_color, 7))
        c.coords(self.dot, left, self.state_y)
        c.itemconfigure(self.state, text=state)
        c.coords(self.state, left + dot_w + gap, self.state_y)
        self.button.set(text=button, enabled=enabled)


class AddCard:
    def __init__(self, app, parent):
        px = app.px
        m, w, h = px(GAP), px(CARD_W), px(CARD_H)
        self.canvas = c = tk.Canvas(parent, width=w + 2 * m, height=h + 2 * m, bg=BG, highlightthickness=0, bd=0,
                                    cursor='hand2')
        c.create_image(0, 0, anchor='nw', image=app.photo(('addcard',), lambda: icons.panel_png(
            w + 2 * m, h + 2 * m, (m, m, m + w, m + h), px(14), border='#CFCCC4', border_width=px(1.5))))
        top = m + (h - px(44 + 12 + 20)) / 2
        cx = m + w / 2
        c.create_image(cx, top, anchor='n', image=app.photo(('plus',), lambda: icons.circle_png(px(44), '#5F5E5A')))
        c.create_text(cx, top + px(21), text='+', fill='#FFFFFF', font=app.f_plus)
        c.create_text(cx, top + px(56), anchor='n', text='New profile', fill='#6B6A65', font=app.f_add)
        c.bind('<ButtonRelease-1>', lambda e: app.new_profile())


class NewProfileDialog:
    def __init__(self, app):
        self.app, self.color, self.result = app, 'blue', None
        px = app.px
        self.top = top = tk.Toplevel(app.root, bg=CARD_BG)
        top.title('New profile')
        top.transient(app.root)
        top.resizable(False, False)
        body = tk.Frame(top, bg=CARD_BG)
        body.pack(padx=px(22), pady=px(22), fill='both')
        tk.Label(body, text='Profile name', font=app.f_label, fg=TEXT, bg=CARD_BG).pack(anchor='w')
        self.name = tk.Entry(body, font=app.f_entry, width=28, relief='flat', bg='#FFFFFF', fg=TEXT,
                             insertbackground=TEXT, highlightthickness=1, highlightbackground='#CFCCC4',
                             highlightcolor=core.COLORS['blue'])
        self.name.pack(fill='x', pady=(px(6), 0), ipady=px(5))
        tk.Label(body, text='For example: Work, Personal, Client-A', font=app.f_tiny, fg=MUTED,
                 bg=CARD_BG).pack(anchor='w', pady=(px(4), 0))
        tk.Label(body, text='Color', font=app.f_label, fg=TEXT, bg=CARD_BG).pack(anchor='w', pady=(px(14), 0))
        size, step = px(28), px(38)
        self.swatches = tk.Canvas(body, width=step * len(core.COLORS), height=size, bg=CARD_BG,
                                  highlightthickness=0, bd=0, cursor='hand2')
        self.swatches.pack(anchor='w', pady=(px(8), 0))
        self.swatch_items = {}
        for i, key in enumerate(core.COLORS):
            item = self.swatches.create_image(i * step, 0, anchor='nw')
            self.swatches.tag_bind(item, '<ButtonRelease-1>', lambda e, k=key: self.pick(k))
            self.swatch_items[key] = item
        self.pick(self.color)
        self.err = tk.Label(body, text='', font=app.f_small, fg='#DC2626', bg=CARD_BG, justify='left',
                            wraplength=px(330))
        self.err.pack(anchor='w', pady=(px(10), 0))
        buttons = tk.Frame(body, bg=CARD_BG)
        buttons.pack(anchor='e', pady=(px(10), 0))
        ttk.Button(buttons, text='Cancel', command=top.destroy).pack(side='left')
        ttk.Button(buttons, text='Create', command=self.create, default='active').pack(side='left', padx=(px(8), 0))
        top.bind('<Return>', lambda e: self.create())
        top.bind('<Escape>', lambda e: top.destroy())
        top.update_idletasks()
        r = app.root
        x = r.winfo_rootx() + (r.winfo_width() - top.winfo_reqwidth()) // 2
        y = r.winfo_rooty() + (r.winfo_height() - top.winfo_reqheight()) // 3
        top.geometry(f'+{max(0, x)}+{max(0, y)}')
        self.name.focus_set()
        top.grab_set()

    def pick(self, key):
        self.color = key
        px = self.app.px
        for k, item in self.swatch_items.items():
            selected = k == key
            img = self.app.photo(('swatch', k, selected), lambda k=k, s=selected: icons.swatch_png(
                px(28), core.COLORS[k], s, ring_width=px(2), gap=px(3)))
            self.swatches.itemconfigure(item, image=img)

    def create(self):
        try:
            self.result = core.create_profile(self.app.b, self.name.get(), self.color)
        except Exception as e:
            self.err.configure(text=str(e))
            return
        self.top.destroy()


class App:
    def __init__(self, backend):
        self.b = backend
        if IS_WIN:
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('ClaudeProfiles.App')
            except Exception:
                pass
        self.root = root = tk.Tk()
        if tk.TkVersion < 8.6:
            root.destroy()
            raise RuntimeError(f'Claude Profiles needs Tk 8.6 or later (this Python has Tk {tk.TkVersion}). '
                               'Install Python from python.org, or run `brew install python-tk`.')
        root.withdraw()
        root.title(APP_TITLE)
        root.configure(bg=BG)
        root.report_callback_exception = lambda *exc: self.show_error(exc[1])
        # Tk on Windows counts 96 px per inch at 100%; macOS already works in points.
        self.scale = root.winfo_fpixels('1i') / 96 if IS_WIN else 1.0
        family = 'Segoe UI' if IS_WIN else tkfont.nametofont('TkDefaultFont').actual('family')

        def font(size, bold=False):
            return tkfont.Font(root=root, family=family, size=-self.px(size), weight='bold' if bold else 'normal')

        self.f_h1, self.f_title, self.f_label = font(20, True), font(15, True), font(13, True)
        self.f_button, self.f_entry, self.f_small = font(13), font(14), font(11.5)
        self.f_tiny, self.f_menu, self.f_plus, self.f_add = font(11), font(17, True), font(26), font(14, True)

        self._images = {}
        self.jobs = queue.Queue()
        self.cards = {}
        self.running = {}
        self.busy_key = None
        self.columns = 0
        self._build()
        root.iconphoto(True, self.icon_image('app', 64), self.icon_image('app', 32))

    # ---- Helpers

    def px(self, n) -> int:
        return max(1, round(n * self.scale))

    def photo(self, key, make):
        img = self._images.get(key)
        if img is None:
            img = tk.PhotoImage(master=self.root, data=base64.b64encode(make()).decode('ascii'))
            self._images[key] = img
        return img

    def icon_image(self, color, size):
        return self.photo(('icon', color, size), lambda: icons.draw_icon(color, self.px(size)))

    def dot_image(self, color, size):
        return self.photo(('dot', color, size), lambda: icons.circle_png(self.px(size), color))

    def pill_image(self, w, h, fill, border, alpha):
        return self.photo(('pill', w, h, fill, border, alpha), lambda: icons.panel_png(
            w, h, (0, 0, w, h), self.px(7), fill, alpha, border=border, border_width=self.px(1)))

    def card_image(self):
        m, w, h = self.px(GAP), self.px(CARD_W), self.px(CARD_H)
        return self.photo(('card',), lambda: icons.panel_png(
            w + 2 * m, h + 2 * m, (m, m, m + w, m + h), self.px(14), CARD_BG, border=CARD_BORDER,
            border_width=self.px(1), shadow=(0.10, self.px(14), self.px(1))))

    def set_status(self, text):
        self.status.configure(text=text)

    def show_error(self, err):
        messagebox.showerror(APP_TITLE, str(err), parent=self.root)

    def confirm(self, text) -> bool:
        return messagebox.askyesno(APP_TITLE, text, icon='warning', parent=self.root)

    def run_bg(self, work, done=None, fail=None):
        def target():
            try:
                result = work()
            except Exception as e:
                self.jobs.put((fail or self.show_error, e))
            else:
                if done:
                    self.jobs.put((done, result))
        threading.Thread(target=target, daemon=True).start()

    # ---- Layout

    def _build(self):
        r, px = self.root, self.px
        r.geometry(f'{px(960)}x{px(580)}')
        r.minsize(px(520), px(400))

        header = tk.Frame(r, bg=HEADER_BG)
        header.pack(fill='x')
        self.header_line = tk.Frame(r, bg=LINE, height=1)
        self.header_line.pack(fill='x')
        inner = tk.Frame(header, bg=HEADER_BG)
        inner.pack(fill='x', padx=px(24), pady=px(16))
        pill_widget(self, inner, '+  New Profile', HEADER_BG, self.new_profile).pack(side='right')
        tk.Label(inner, image=self.icon_image('app', 42), bg=HEADER_BG).pack(side='left')
        titles = tk.Frame(inner, bg=HEADER_BG)
        titles.pack(side='left', padx=(px(14), 0))
        tk.Label(titles, text=APP_TITLE, font=self.f_h1, fg=TEXT, bg=HEADER_BG).pack(anchor='w')
        tk.Label(titles, text='Isolated Claude Desktop accounts, side by side', font=self.f_tiny, fg=MUTED,
                 bg=HEADER_BG).pack(anchor='w')

        self.banner = tk.Frame(r, bg=BANNER_BG, highlightthickness=0)
        banner_in = tk.Frame(self.banner, bg=BANNER_BG)
        banner_in.pack(fill='x', padx=px(24), pady=px(8))
        pill_widget(self, banner_in, 'Restore', BANNER_BG, self.restore_route, padx=12, height=26).pack(side='right')
        self.banner_text = tk.Label(banner_in, font=self.f_small, fg=BANNER_FG, bg=BANNER_BG, justify='left',
                                    anchor='w')
        self.banner_text.pack(side='left', fill='x', expand=True)

        self.status = tk.Label(r, font=self.f_tiny, fg=MUTED, bg=BG, anchor='w')
        self.status.pack(side='bottom', fill='x', padx=px(26), pady=(0, px(10)))

        self.body = body = tk.Frame(r, bg=BG)
        body.pack(fill='both', expand=True)
        self.scroll = tk.Canvas(body, bg=BG, highlightthickness=0, bd=0)
        self.vbar = ttk.Scrollbar(body, orient='vertical', command=self.scroll.yview)
        self.scroll.configure(yscrollcommand=self._on_scroll)
        self.scroll.pack(side='left', fill='both', expand=True)
        self.grid = tk.Frame(self.scroll, bg=BG)
        self.scroll.create_window(px(16), px(4), window=self.grid, anchor='nw')
        self.grid.bind('<Configure>', lambda e: self.scroll.configure(
            scrollregion=(0, 0, e.width, e.height + self.px(12))))
        self.scroll.bind('<Configure>', lambda e: self._layout())
        r.bind_all('<MouseWheel>', self._wheel)

    def _on_scroll(self, first, last):
        self.vbar.set(first, last)
        if float(first) <= 0 and float(last) >= 1:
            self.vbar.pack_forget()
        elif not self.vbar.winfo_ismapped():
            self.vbar.pack(side='right', fill='y', before=self.scroll)

    def _wheel(self, e):
        if self.vbar.winfo_ismapped():
            self.scroll.yview_scroll(int(-e.delta / 120) if IS_WIN else -e.delta, 'units')

    def _layout(self, force=False):
        cell = self.px(CARD_W + 2 * GAP)
        cols = max(1, (self.scroll.winfo_width() - self.px(32)) // cell)
        if cols == self.columns and not force:
            return
        self.columns = cols
        for i, w in enumerate(self.grid.winfo_children()):
            w.grid(row=i // cols, column=i % cols, sticky='nw')

    def rebuild(self):
        for w in self.grid.winfo_children():
            w.destroy()
        self.cards = {}
        for p in [core.default_profile(self.b), *core.list_profiles(self.b)]:
            card = Card(self, self.grid, p)
            self.cards[p.key] = card
        AddCard(self, self.grid)
        self._layout(force=True)
        self.update_cards()

    def update_cards(self):
        for key, card in self.cards.items():
            if key == self.busy_key:
                card.show(DOT_BUSY, 'Starting...', 'Launch', False)
            elif key in self.running:
                card.show(DOT_RUNNING, 'Running', 'Focus', True)
            else:
                card.show(DOT_STOPPED, 'Stopped', 'Launch', True)

    def update_banner(self):
        slug = self.b.route_slug() if self.b.supports_signin_route else ''
        if slug:
            p = next((p for p in core.list_profiles(self.b) if p.slug == slug), None)
            name = p.name if p else slug
            self.banner_text.configure(
                text=f"claude:// sign-in links currently open the '{name}' profile. Click Restore after signing in.")
            if not self.banner.winfo_ismapped():
                self.banner.pack(fill='x', after=self.header_line)
        else:
            self.banner.pack_forget()

    # ---- Timers

    def _drain(self):
        try:
            while True:
                fn, arg = self.jobs.get_nowait()
                try:
                    fn(arg)
                except Exception as e:
                    self.show_error(e)
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def refresh_running(self):
        try:
            self.running = self.b.running_map()
        except Exception:
            pass
        self.update_cards()
        # Re-apply taskbar colors: Claude recreates its window, e.g. when reopened from the tray.
        for key, pid in self.running.items():
            card = self.cards.get(key)
            if card and key != self.busy_key:
                try:
                    core.brand_profile(self.b, card.profile, pid)
                except Exception:
                    pass

    def _tick(self):
        self.refresh_running()
        self.root.after(3000, self._tick)

    # ---- Actions

    def launch(self, profile):
        if self.busy_key:
            self.set_status('Still starting a profile. Try again in a moment.')
            return
        self.busy_key = profile.key
        self.update_cards()

        def done(msg):
            self.busy_key = None
            self.refresh_running()
            if msg:
                self.set_status(msg)

        def fail(err):
            self.busy_key = None
            self.update_cards()
            self.show_error(err)

        self.run_bg(lambda: core.open_profile(self.b, profile), done, fail)

    def new_profile(self):
        dlg = NewProfileDialog(self)
        self.root.wait_window(dlg.top)
        if dlg.result:
            self.rebuild()
            self.set_status(f"Created '{dlg.result.name}'. First sign-in: close your other Claude windows first.")

    def show_menu(self, profile, event):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label='Open data folder', command=lambda: self.open_data_folder(profile))
        if not profile.is_default:
            colors = tk.Menu(menu, tearoff=0)
            self._color_var = tk.StringVar(master=self.root, value=profile.color)
            for key, label in core.COLOR_LABELS.items():
                colors.add_radiobutton(label=label, value=key, variable=self._color_var, compound='left',
                                       image=self.dot_image(core.COLORS[key], 12),
                                       command=lambda k=key: self.set_color(profile, k))
            menu.add_cascade(label='Color', menu=colors)
            menu.add_command(label=self.b.shortcut_menu_label, command=lambda: self.create_shortcut(profile))
            if self.b.supports_signin_route:
                menu.add_command(label='Receive Google/SSO sign-in link (experimental)',
                                 command=lambda: self.route_signin(profile))
            menu.add_separator()
            menu.add_command(label='Quit profile', command=lambda: self.quit_profile(profile))
            menu.add_command(label='Delete profile...', command=lambda: self.delete_profile(profile))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def open_data_folder(self, profile):
        def work():
            d = profile.data_dir or self.b.default_data_dir()
            if d and d.exists():
                self.b.open_folder(d)
            else:
                raise RuntimeError('The data folder does not exist yet. Launch the profile once first.')
        self.run_bg(work)

    def set_color(self, profile, color):
        profile.color = color
        core.save_meta(profile)
        if self.b.profile_shortcut_path(profile).exists():
            self.run_bg(lambda: (self.b.remove_profile_shortcut(profile), core.create_profile_shortcut(self.b, profile)))
        self.rebuild()

    def create_shortcut(self, profile):
        self.set_status('Creating shortcut...')
        self.run_bg(lambda: core.create_profile_shortcut(self.b, profile), lambda path: self.set_status(f'Created {path}'))

    def route_signin(self, profile):
        msg = ("Only needed when a Google/SSO sign-in in the browser comes back to the wrong Claude window.\n\n"
               f"This points claude:// links (in your HKCU registry) at the '{profile.name}' profile. "
               "Open this profile first, sign in, then click 'Restore' on the yellow bar.\n\nContinue?")
        if self.confirm(msg):
            self.b.set_signin_route(profile)
            self.update_banner()

    def restore_route(self):
        self.b.clear_signin_route()
        self.update_banner()
        self.set_status('claude:// sign-in links open your default Claude again.')

    def quit_profile(self, profile):
        pid = self.b.running_map().get(profile.key)
        if not pid:
            self.set_status(f'{profile.name} is not running.')
            return
        if self.confirm(f"Quit Claude '{profile.name}'?"):
            self.b.stop(pid)
            self.set_status(f'{profile.name} was closed.')
            self.root.after(1000, self.refresh_running)

    def delete_profile(self, profile):
        if profile.key in self.b.running_map():
            raise RuntimeError(f"Quit '{profile.name}' before deleting it.")
        msg = (f"Permanently delete the '{profile.name}' profile?\n\nThis deletes:\n"
               f"  {profile.data_dir}  (sign-in, chats, settings)\n")
        if (profile.config_dir / core.OWNER_MARK).is_file():
            msg += f'  {profile.config_dir}  (Claude Code data)\n'
        msg += '\nThis cannot be undone. Your chats on claude.ai stay in the account.'
        if self.confirm(msg):
            core.delete_profile(self.b, profile)
            self.rebuild()
            self.set_status(f'Deleted {profile.name}.')

    # ---- Run

    def run(self) -> int:
        r = self.root
        r.update_idletasks()
        w, h = self.px(960), self.px(580)
        r.geometry(f'{w}x{h}+{max(0, (r.winfo_screenwidth() - w) // 2)}+{max(0, (r.winfo_screenheight() - h) // 3)}')
        self.rebuild()
        self.update_banner()
        r.deiconify()
        self.set_status('Looking for Claude Desktop...')

        def migrated(messages):
            if messages:
                self.rebuild()
                self.update_banner()
                messagebox.showinfo(APP_TITLE, '\n'.join(messages), parent=self.root)
            self.run_bg(self.b.footer_text, self.set_status)
        self.run_bg(self.b.migrate, migrated)
        r.after(100, self._drain)
        r.after(0, self._tick)
        r.mainloop()
        return 0


def run(backend) -> int:
    return App(backend).run()
