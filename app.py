#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChefBot Telegram Server (All-in-One)
Flask + SQLAlchemy + MySQL + Gemini (opsional) + Rekomendasi + Smalltalk
Perbaikan:
- Intent smalltalk (tanpa fallback)
- Intent rekomendasi ("bingung mau masak apa") dari DB
- /pantang tambah <bahan>: partial match + auto-create bahan
- Penguatan /addmenu & /addmenulink error handling/logging
"""

import os, json, math, logging, re, random
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

from sqlalchemy import (
    create_engine, Column, BigInteger, Integer, String, Text, Enum,
    ForeignKey, DECIMAL, DateTime, TIMESTAMP, CheckConstraint, func
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker, Session

# ============== Gemini (opsional) ==========
try:
    import google.generativeai as genai
except Exception:
    genai = None

# ============================================================
#  LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
log = logging.getLogger("chefbot")

# ============================================================
#  ENV & CONFIG
# ============================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "whk-12345").strip()

DB_URL = (
    os.getenv("DB_URL", "").strip()
    or "mysql+pymysql://chefbot_user:StrongPass@127.0.0.1:3306/chefbot?charset=utf8mb4"
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "text-embedding-004")

GEMINI_MODEL = None
if GEMINI_API_KEY and genai is not None:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        GEMINI_MODEL = genai.GenerativeModel("gemini-2.5-flash-lite")
    except Exception as e:
        log.warning("Gagal inisialisasi Gemini: %s", e)

if not BOT_TOKEN:
    log.warning("BOT_TOKEN belum di-set di .env")

# ============================================================
#  DB SETUP
# ============================================================
class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    telegram_user_id = Column(BigInteger, primary_key=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default="CURRENT_TIMESTAMP")
    riwayat = relationship("UserMenuRiwayat", back_populates="user")
    rating = relationship("UserMenuRating", back_populates="user")
    pantangan = relationship("UserBahanPantang", back_populates="user")

class Bahan(Base):
    __tablename__ = "bahan"
    id_bahan = Column(BigInteger, primary_key=True, autoincrement=True)
    nama_bahan = Column(String(120), nullable=False, unique=True)
    satuan_bahan = Column(String(30), nullable=False)
    menu_bahan = relationship("MenuBahan", back_populates="bahan")
    pantangan = relationship("UserBahanPantang", back_populates="bahan")

class Menu(Base):
    __tablename__ = "menu"
    id_menu = Column(BigInteger, primary_key=True, autoincrement=True)
    nama_masakan = Column(String(200), nullable=False, unique=True)
    tingkat_kesulitan = Column(
        Enum("easy","medium","hard", name="tingkat_kesulitan_enum"),
        nullable=False, server_default="easy"
    )
    source_url = Column(String(255), nullable=True)
    langkah = relationship("MenuLangkah", back_populates="menu")
    bahan = relationship("MenuBahan", back_populates="menu")
    riwayat = relationship("UserMenuRiwayat", back_populates="menu")
    rating = relationship("UserMenuRating", back_populates="menu")

class MenuLangkah(Base):
    __tablename__ = "menu_langkah"
    id_langkah = Column(BigInteger, primary_key=True, autoincrement=True)
    id_menu = Column(BigInteger, ForeignKey("menu.id_menu"), nullable=False)
    langkah_no = Column(Integer, nullable=False)
    deskripsi = Column(Text, nullable=False)
    menu = relationship("Menu", back_populates="langkah")

class MenuBahan(Base):
    __tablename__ = "menu_bahan"
    id_menu = Column(BigInteger, ForeignKey("menu.id_menu"), primary_key=True)
    id_bahan = Column(BigInteger, ForeignKey("bahan.id_bahan"), primary_key=True)
    banyak_bahan = Column(DECIMAL(8,2), nullable=False)
    catatan = Column(String(120), nullable=True)
    menu = relationship("Menu", back_populates="bahan")
    bahan = relationship("Bahan", back_populates="menu_bahan")

class UserMenuRiwayat(Base):
    __tablename__ = "user_menu_riwayat"
    id_riwayat = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_user_id = Column(BigInteger, ForeignKey("users.telegram_user_id"), nullable=False)
    id_menu = Column(BigInteger, ForeignKey("menu.id_menu"), nullable=False)
    waktu = Column(DateTime, nullable=False, default=datetime.utcnow)
    keterangan = Column(String(200), nullable=True)
    user = relationship("User", back_populates="riwayat")
    menu = relationship("Menu", back_populates="riwayat")

class UserMenuRating(Base):
    __tablename__ = "user_menu_rating"
    telegram_user_id = Column(BigInteger, ForeignKey("users.telegram_user_id"), primary_key=True)
    id_menu = Column(BigInteger, ForeignKey("menu.id_menu"), primary_key=True)
    rating_menu = Column(Integer, nullable=False)
    review = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default="CURRENT_TIMESTAMP")
    __table_args__ = (CheckConstraint("rating_menu BETWEEN 1 AND 5", name="chk_rating_menu"),)
    user = relationship("User", back_populates="rating")
    menu = relationship("Menu", back_populates="rating")

class UserBahanPantang(Base):
    __tablename__ = "user_bahan_pantang"
    telegram_user_id = Column(BigInteger, ForeignKey("users.telegram_user_id"), primary_key=True)
    id_bahan = Column(BigInteger, ForeignKey("bahan.id_bahan"), primary_key=True)
    jenis = Column(Enum("pantangan","alergi", name="jenis_pantang_enum"), nullable=False)
    note = Column(String(120), nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default="CURRENT_TIMESTAMP")
    user = relationship("User", back_populates="pantangan")
    bahan = relationship("Bahan", back_populates="pantangan")

class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    source = Column(String(255), nullable=False)
    title = Column(String(255), nullable=False)
    page_no = Column(Integer, nullable=True)
    chunk_text = Column(Text, nullable=False)
    embedding_json = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP, nullable=False, server_default="CURRENT_TIMESTAMP")

engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=3600, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

@contextmanager
def get_session() -> Session:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        log.exception("DB error: %s", e)
        raise
    finally:
        session.close()

# ============================================================
#  TELEGRAM HELPERS
# ============================================================
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""

def send_message(chat_id: int, text: str, parse_mode: Optional[str] = "Markdown") -> None:
    if not BOT_TOKEN:
        log.error("BOT_TOKEN kosong, tidak bisa kirim pesan.")
        return
    url = f"{TELEGRAM_API_BASE}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            log.warning("sendMessage gagal: %s - %s", resp.status_code, resp.text)
    except Exception as e:
        log.exception("sendMessage exception: %s", e)

def send_message_with_inline_keyboard(chat_id: int, text: str,
                                      keyboard: List[List[Dict[str,str]]],
                                      parse_mode: str = "Markdown") -> None:
    if not BOT_TOKEN:
        log.error("BOT_TOKEN kosong.")
        return
    url = f"{TELEGRAM_API_BASE}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "reply_markup": {"inline_keyboard": keyboard}}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            log.warning("sendMessage (inline) gagal: %s - %s", resp.status_code, resp.text)
    except Exception as e:
        log.exception("sendMessage_with_inline_keyboard exception: %s", e)

def send_chat_action(chat_id: int, action: str = "typing") -> None:
    if not BOT_TOKEN: return
    url = f"{TELEGRAM_API_BASE}/sendChatAction"
    try:
        requests.post(url, json={"chat_id": chat_id, "action": action}, timeout=5)
    except Exception:
        pass

def answer_callback_query(callback_query_id: str, text: Optional[str] = None, show_alert: bool=False) -> None:
    if not BOT_TOKEN: return
    url = f"{TELEGRAM_API_BASE}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text: payload["text"]=text
    if show_alert: payload["show_alert"]=True
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception:
        pass

# ============================================================
#  GEMINI HELPERS (opsional)
# ============================================================
def ask_gemini(prompt: str) -> str:
    if GEMINI_MODEL is None:
        return "Maaf, AI belum dikonfigurasi (GEMINI_API_KEY belum di-set)."
    try:
        resp = GEMINI_MODEL.generate_content(prompt)
        return (getattr(resp, "text", "") or "").strip() or "Maaf, aku tidak mendapatkan jawaban dari model."
    except Exception as e:
        log.exception("Gemini.generate_content error: %s", e)
        return "Maaf, sedang ada kendala saat menghubungi AI."

def embed_text(text: str) -> Optional[List[float]]:
    if GEMINI_MODEL is None or genai is None: return None
    try:
        res = genai.embed_content(model=GEMINI_EMBED_MODEL, content=text)
        if isinstance(res, dict) and "embedding" in res: return res["embedding"]
        if hasattr(res, "embedding"): return res.embedding
        return None
    except Exception as e:
        log.exception("Gemini.embed_content error: %s", e)
        return None

def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a)!=len(b): return 0.0
    dot = sum(x*y for x,y in zip(a,b))
    na = math.sqrt(sum(x*x for x in a)); nb = math.sqrt(sum(x*x for x in b))
    return 0.0 if (na==0.0 or nb==0.0) else dot/(na*nb)

# ============================================================
#  DOMAIN LOGIC
# ============================================================
MENU_SEARCH_STOPWORDS = {
    "resep","tolong","buatkan","bikinkan","bikin","minta","dong","ya","yg","yang",
    "untuk","buat","cara","masak","masakan","menu","rekomendasi","khas","versi",
    "aku","saya","sedikit","banget","please"
}

def _normalize_name(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+"," ", s)
    tokens = [w for w in s.split() if w and w not in MENU_SEARCH_STOPWORDS]
    return " ".join(tokens).strip()

def find_relevant_menus(session: Session, query_text: str, limit: int = 3) -> List[Menu]:
    q_raw = (query_text or "").strip()
    if not q_raw: return []
    q_norm = _normalize_name(q_raw)
    if not q_norm: return []
    q_tokens = set(q_norm.split())
    candidates: List[Menu] = session.query(Menu).all()
    if not candidates: return []
    def score(menu: Menu)->int:
        name_norm = _normalize_name(menu.nama_masakan or "")
        if not name_norm: return 0
        s=0
        if name_norm==q_norm: s+=1000
        s+= 20*len(q_tokens & set(name_norm.split()))
        if q_norm in name_norm or name_norm in q_norm: s+=50
        return s
    scored=[(m,score(m)) for m in candidates]
    scored=[x for x in scored if x[1]>0]
    scored.sort(key=lambda x:(-x[1], (x[0].nama_masakan or "").lower()))
    return [m for m,_ in scored[:limit]]

def ensure_user(session: Session, telegram_user_id: int) -> User:
    user = session.get(User, telegram_user_id)
    if not user:
        user = User(telegram_user_id=telegram_user_id)
        session.add(user)
        log.info("User baru: %s", telegram_user_id)
    return user

def get_user_pantang_map(session: Session, telegram_user_id: int) -> Dict[int, UserBahanPantang]:
    rows = (session.query(UserBahanPantang)
            .join(Bahan, UserBahanPantang.id_bahan==Bahan.id_bahan)
            .filter(UserBahanPantang.telegram_user_id==telegram_user_id).all())
    return {p.id_bahan:p for p in rows}

def build_pantang_warning_for_menus(menus: List[Menu], pantang_map: Optional[Dict[int,UserBahanPantang]])->str:
    if not pantang_map or not menus: return ""
    lines=[]
    for menu in menus:
        matched=[]
        seen=set()
        for mb in menu.bahan:
            p = pantang_map.get(mb.id_bahan)
            if p and mb.id_bahan not in seen:
                seen.add(mb.id_bahan); matched.append(p)
        if matched:
            lines.append(f"⚠️ *Pantangan/alergi untuk {menu.nama_masakan}* (ID {menu.id_menu}):")
            for p in matched:
                note=f", catatan: {p.note}" if p.note else ""
                lines.append(f"- {p.bahan.nama_bahan} ({p.jenis}{note})")
            lines.append("")
    if not lines: return ""
    lines.append("⚠️ *Catatan:* pilih resep lain atau ganti bahan yang menjadi pantangan/alergi.")
    return "\n".join(lines)

def build_history_text(session: Session, telegram_user_id: int, limit:int=5)->str:
    rows=(session.query(UserMenuRiwayat)
          .join(Menu, UserMenuRiwayat.id_menu==Menu.id_menu)
          .filter(UserMenuRiwayat.telegram_user_id==telegram_user_id)
          .order_by(UserMenuRiwayat.waktu.desc()).limit(limit).all())
    if not rows:
        return ("Belum ada riwayat masakan yang pernah kamu lihat.\n"
                "Coba kirim nama masakan atau bahan dulu ya 😊")
    lines=["*Riwayat masakan terakhir kamu:*"]
    for r in rows:
        rating=(session.query(UserMenuRating)
                .filter(UserMenuRating.telegram_user_id==telegram_user_id,
                        UserMenuRating.id_menu==r.id_menu).one_or_none())
        rating_str=f"{rating.rating_menu}/5" if rating else "-"
        lines.append(f"- [{r.menu.id_menu}] {r.menu.nama_masakan} (⭐ {rating_str}, {r.waktu.strftime('%d-%m-%Y %H:%M')})")
    lines.append("\nUntuk memberi rating manual:\n`/rating <id_menu> <1-5> [review]`")
    return "\n".join(lines)

def build_menu_list_text(session: Session, limit:int=50)->str:
    menus=(session.query(Menu).order_by(Menu.id_menu.asc()).limit(limit+1).all())
    lines=[]
    if menus:
        lines.append("*Daftar menu di database ChefBot (urut ID):*")
        for m in menus[:limit]:
            src = f", sumber: {m.source_url}" if m.source_url else ""
            lines.append(f"- ID {m.id_menu}: {m.nama_masakan} (kesulitan: {m.tingkat_kesulitan}{src})")
        if len(menus)>limit: lines.append(f"... dan {len(menus)-limit} menu lainnya.")
        lines.append("")
    if not lines:
        lines.append("Belum ada data menu tersimpan di ChefBot.")
    return "\n".join(lines)

# ------- Rekomendasi -------
RECOM_INTENT_PATTERNS = [
    r"\bbingung\b.*\bmasak\b",
    r"\bkasih(kan)?\s*ide\b",
    r"\brekom(endasi)?\b",
    r"\bmasak apa\b",
]

def is_recommendation_intent(text: str) -> bool:
    t=(text or "").lower()
    return any(re.search(p, t) for p in RECOM_INTENT_PATTERNS)

def get_recommendation_list(session: Session, limit:int=5) -> List[Menu]:
    # ambil menu acak; kalau DB besar bisa ganti ke rating terbanyak
    count = session.query(func.count(Menu.id_menu)).scalar() or 0
    if count==0: return []
    # ambil id range, lalu sample random
    ids = [m.id_menu for m in session.query(Menu.id_menu).all()]
    random.shuffle(ids); ids = ids[:min(limit*2, len(ids))]
    menus = (session.query(Menu).filter(Menu.id_menu.in_(ids)).all())
    random.shuffle(menus)
    return menus[:limit]

def build_recommendation_message(menus: List[Menu]) -> Tuple[str, List[List[Dict[str,str]]]]:
    if not menus:
        return "Belum ada menu di database untuk direkomendasikan.", []
    lines=["Berikut beberapa *ide masak* untukmu:"]
    for m in menus:
        lines.append(f"- [{m.id_menu}] {m.nama_masakan} (kesulitan: {m.tingkat_kesulitan})")
    kb=[[{"text":"📜 Riwayat saya","callback_data":"history"}],
        [{"text":"📋 Daftar menu","callback_data":"menu_list"}]]
    return "\n".join(lines), kb

# ------- Smalltalk -------
SMALLTALK_PATTERNS = {
    "halo": [r"\bhalo+\b", r"\bhai+\b", r"\bhallo+\b", r"\bselamat\s*(pagi|siang|sore|malam)\b"],
    "bye":  [r"\bbye+\b", r"\bdada(h)?\b", r"\bsampai jumpa\b", r"\bsee you\b"],
    "thanks":[r"\bterima kasih\b", r"\bmakasih\b", r"\bthank(s| you)\b"],
}

def is_smalltalk(text:str) -> Optional[str]:
    t=(text or "").lower()
    for label, pats in SMALLTALK_PATTERNS.items():
        if any(re.search(p, t) for p in pats):
            return label
    return None

def smalltalk_reply(label:str) -> Tuple[str, List[List[Dict[str,str]]]]:
    if label=="halo":
        text=("Halo! 👋 Aku *ChefBot*.\n"
              "Mau cari resep apa hari ini? Kamu bisa kirim nama masakan atau pilih tombol di bawah.")
        kb=[[{"text":"🎲 Rekomendasi","callback_data":"rekomendasi"}],
            [{"text":"📜 Riwayat saya","callback_data":"history"}],
            [{"text":"📋 Daftar menu","callback_data":"menu_list"}],
            [{"text":"⚙️ Atur pantangan","callback_data":"pantang_manage"}]]
        return text, kb
    if label=="bye":
        text=("Sampai jumpa! 👋 Kalau butuh ide masak, balik lagi ya.")
        kb=[[{"text":"🎲 Rekomendasi","callback_data":"rekomendasi"}],
            [{"text":"📋 Daftar menu","callback_data":"menu_list"}]]
        return text, kb
    if label=="thanks":
        text=("Sama-sama! 😊 Semoga membantu.\nKalau mau ide lain, tekan tombol di bawah.")
        kb=[[{"text":"🎲 Rekomendasi","callback_data":"rekomendasi"}],
            [{"text":"📜 Riwayat saya","callback_data":"history"}]]
        return text, kb
    return "Hai!", []

# ------- Simpan menu dari JSON -------
def save_generated_menu_to_db(session: Session, recipe: dict, source_url: Optional[str]=None) -> Tuple[Optional[Menu], str]:
    try:
        nama_masakan = (recipe.get("nama_masakan") or "").strip()
        tingkat = (recipe.get("tingkat_kesulitan") or "easy").strip().lower()
        bahan_list = recipe.get("bahan") or []
        langkah_list = recipe.get("langkah") or []
        if not nama_masakan: return None, "Nama masakan kosong di JSON resep."
        if tingkat not in ("easy","medium","hard"): tingkat="easy"

        existing = (session.query(Menu).filter(Menu.nama_masakan.ilike(nama_masakan)).one_or_none())
        if existing:
            menu_obj = existing
            menu_obj.tingkat_kesulitan = tingkat
            if source_url: menu_obj.source_url = source_url
            msg_prefix = "Menu sudah ada, komponen diperbarui."
        else:
            menu_obj = Menu(nama_masakan=nama_masakan, tingkat_kesulitan=tingkat, source_url=source_url)
            session.add(menu_obj); session.flush()
            msg_prefix = "Menu baru ditambahkan."

        if existing:
            session.query(MenuBahan).filter(MenuBahan.id_menu==menu_obj.id_menu).delete()
            session.query(MenuLangkah).filter(MenuLangkah.id_menu==menu_obj.id_menu).delete()

        for b in bahan_list:
            nama = (b.get("nama") or "").strip()
            satuan = (b.get("satuan") or "").strip() or "unit"
            jumlah = b.get("jumlah") or 0
            if not nama: continue
            bahan_obj = (session.query(Bahan).filter(Bahan.nama_bahan.ilike(nama)).one_or_none())
            if not bahan_obj:
                bahan_obj = Bahan(nama_bahan=nama, satuan_bahan=satuan)
                session.add(bahan_obj); session.flush()
            session.add(MenuBahan(id_menu=menu_obj.id_menu, id_bahan=bahan_obj.id_bahan,
                                  banyak_bahan=jumlah, catatan=None))
        for i, step in enumerate(langkah_list, start=1):
            step_text = str(step).strip()
            if step_text:
                session.add(MenuLangkah(id_menu=menu_obj.id_menu, langkah_no=i, deskripsi=step_text))
        return menu_obj, f"{msg_prefix} (ID {menu_obj.id_menu})"
    except Exception as e:
        log.exception("Simpan menu error: %s", e)
        return None, "Kesalahan saat menyimpan menu ke database."

# ---------- /pantang (perbaikan) ----------
def handle_pantang_command(session: Session, telegram_user_id:int, text:str) -> str:
    parts = text.strip().split(maxsplit=2)
    subcmd = parts[1].lower() if len(parts)>1 else "list"
    user = ensure_user(session, telegram_user_id)

    if subcmd in ("list",):
        items=(session.query(UserBahanPantang)
               .join(Bahan, UserBahanPantang.id_bahan==Bahan.id_bahan)
               .filter(UserBahanPantang.telegram_user_id==user.telegram_user_id)
               .order_by(Bahan.nama_bahan.asc()).all())
        if not items:
            return ("Kamu belum punya data pantangan/alergi.\n"
                    "Gunakan: `/pantang tambah <nama_bahan> [pantangan|alergi]`")
        lines=["*Daftar pantangan/alergi kamu:*"]
        for p in items:
            note=f", catatan: {p.note}" if p.note else ""
            lines.append(f"- {p.bahan.nama_bahan} ({p.jenis}{note})")
        return "\n".join(lines)

    if subcmd in ("tambah","add","+"):
        if len(parts)<3:
            return ("Format: `/pantang tambah <nama_bahan> [pantangan|alergi]`\n"
                    "Contoh: `/pantang tambah udang alergi`")
        raw = parts[2].strip()
        toks = raw.split()
        last = toks[-1].lower() if toks else ""
        jenis = "pantangan" if last not in ("pantangan","alergi") else last
        nama_bahan_in = raw if jenis=="pantangan" else " ".join(toks[:-1]).strip()
        if not nama_bahan_in: return "Nama bahan tidak boleh kosong."

        candidates: List[Bahan] = (session.query(Bahan)
            .filter(Bahan.nama_bahan.ilike(f"%{nama_bahan_in}%"))
            .order_by(Bahan.nama_bahan.asc()).all())
        created=[]
        if not candidates:
            new_bahan=Bahan(nama_bahan=nama_bahan_in, satuan_bahan="unit")
            session.add(new_bahan); session.flush()
            candidates=[new_bahan]; created.append(new_bahan)

        added, updated = 0, 0
        for b in candidates:
            existing=(session.query(UserBahanPantang)
                      .filter(UserBahanPantang.telegram_user_id==user.telegram_user_id,
                              UserBahanPantang.id_bahan==b.id_bahan).one_or_none())
            if existing:
                if existing.jenis != jenis:
                    existing.jenis = jenis
                updated+=1
            else:
                session.add(UserBahanPantang(telegram_user_id=user.telegram_user_id,
                                             id_bahan=b.id_bahan, jenis=jenis))
                added+=1

        if created and len(candidates)==1:
            return (f"Bahan *{created[0].nama_bahan}* belum ada, sudah dibuat (satuan: unit) "
                    f"dan ditandai sebagai *{jenis}*.")
        matched=", ".join([b.nama_bahan for b in candidates])
        return (f"Pantangan *{jenis}* diterapkan untuk {len(candidates)} bahan: {matched}.\n"
                f"(ditambah: {added}, diperbarui: {updated})")

    if subcmd in ("hapus","del","delete","-"):
        if len(parts)<3: return "Format: `/pantang hapus <nama_bahan>`"
        nama_bahan_in = parts[2].strip()
        bahan_rows=(session.query(Bahan).filter(Bahan.nama_bahan.ilike(f"%{nama_bahan_in}%")).all())
        if not bahan_rows: return f"Tidak ada bahan yang cocok dengan '{nama_bahan_in}'."
        deleted=(session.query(UserBahanPantang)
                 .filter(UserBahanPantang.telegram_user_id==user.telegram_user_id,
                         UserBahanPantang.id_bahan.in_([b.id_bahan for b in bahan_rows]))
                 .delete(synchronize_session=False))
        names=", ".join([b.nama_bahan for b in bahan_rows])
        return f"Pantangan untuk ({names}) dihapus. (baris terhapus: {deleted})"

    return ("Perintah /pantang tidak dikenal.\n"
            "Gunakan:\n"
            "- `/pantang` atau `/pantang list`\n"
            "- `/pantang tambah <nama_bahan> [pantangan|alergi]`\n"
            "- `/pantang hapus <nama_bahan>`")

# ---------- /rating ----------
def handle_rating_command(session: Session, telegram_user_id:int, text:str)->str:
    parts=text.strip().split(maxsplit=3)
    if len(parts)<3:
        return ("Format: `/rating <id_menu> <1-5> [review]`\n"
                "Contoh: `/rating 3 5 Enak dan mudah diikuti`")
    try:
        id_menu=int(parts[1]); nilai=int(parts[2])
    except ValueError:
        return "Format salah. Contoh: `/rating 3 5 enak banget`"
    if not (1<=nilai<=5): return "Nilai rating harus 1–5."
    review = parts[3].strip() if len(parts)>=4 else None
    menu = session.get(Menu, id_menu)
    if not menu: return f"Menu id={id_menu} tidak ditemukan."
    user=ensure_user(session, telegram_user_id)
    existing=(session.query(UserMenuRating)
              .filter(UserMenuRating.telegram_user_id==user.telegram_user_id,
                      UserMenuRating.id_menu==id_menu).one_or_none())
    if existing:
        existing.rating_menu=nilai; existing.review=review; action="diperbarui"
    else:
        session.add(UserMenuRating(telegram_user_id=user.telegram_user_id,
                                   id_menu=id_menu, rating_menu=nilai, review=review))
        action="disimpan"
    return f"Rating kamu untuk *{menu.nama_masakan}* (ID {menu.id_menu}) {action} dengan nilai *{nilai}*."

# ---------- HELP ----------
def get_help_text()->str:
    return (
        "*Cara pakai ChefBot* 👩‍🍳\n\n"
        "1️⃣ Kirim nama masakan (mis. `sapi rica rica`, `nasi goreng sosis`).\n"
        "2️⃣ Atau kirim bahan yang kamu punya (mis. `aku punya telur, nasi, kecap`).\n"
        "3️⃣ ChefBot akan memberi ide resep + langkah.\n\n"
        "Pantangan & alergi:\n"
        "- `/pantang tambah <nama_bahan> [pantangan|alergi]`\n"
        "- `/pantang hapus <nama_bahan>`\n"
        "- `/pantang list`\n\n"
        "Rating:\n"
        "- `/rating <id_menu> <1-5> [review]`\n\n"
        "Menu:\n"
        "- `/menu` untuk daftar menu\n"
        "- `/history` untuk riwayat\n\n"
        "Tambah menu baru:\n"
        "- `/addmenu <deskripsi>` (via AI bila tersedia)\n"
        "- `/addmenulink <URL>` (ekstrak dari halaman resep)"
    )

# ---------- BUILD PROMPT (ringkas) ----------
def build_chefbot_prompt(user_text:str, menu_context:str="")->str:
    return (
        "Kamu adalah ChefBot, jawablah singkat, jelas, terstruktur, jangan jawab pertanyaan apapun kecuali terkait masakan atau makanan.\n\n"
        f"MENU DB:\n{menu_context}\n\n"
        f"USER: {user_text}\n"
        "Jika diminta resep, berikan bahan (dengan takaran wajar) dan langkah berurutan."
    )

# ---------- Generate Answer ----------
def generate_answer_for_user(session: Session, telegram_user_id:int, text:str):
    pantang_map = get_user_pantang_map(session, telegram_user_id)
    menus = find_relevant_menus(session, text, limit=3)
    # siapkan konteks menu
    menu_context=""
    if menus:
        lines=[]
        for m in menus:
            src=f" | source: {m.source_url}" if m.source_url else ""
            lines.append(f"Menu ID: {m.id_menu} | Nama: {m.nama_masakan} | Kesulitan: {m.tingkat_kesulitan}{src}")
            lines.append("Bahan:")
            for mb in m.bahan:
                extra=""
                if pantang_map and mb.id_bahan in pantang_map:
                    p=pantang_map[mb.id_bahan]; note=f", catatan: {p.note}" if p.note else ""
                    extra = f" [PANTANG_USER:{p.jenis}{note}]"
                lines.append(f"- {mb.banyak_bahan} {mb.bahan.satuan_bahan} {mb.bahan.nama_bahan}{extra}")
            langkah_sorted=sorted(m.langkah, key=lambda x:x.langkah_no)
            lines.append("Langkah:")
            for l in langkah_sorted: lines.append(f"{l.langkah_no}. {l.deskripsi}")
            lines.append("")
        menu_context="\n".join(lines)

    no_context = not menus
    prompt = build_chefbot_prompt(text, menu_context=menu_context)
    answer = ask_gemini(prompt) if GEMINI_MODEL else (
        "Berikut ringkasan resep dari database:\n\n"+menu_context if menu_context else
        "Maaf, aku belum menemukan resep spesifik di database untuk pesanmu."
    )

    if menus and menus[0].source_url:
        answer += f"\n\n(Sumber asli resep: {menus[0].source_url})"

    warning_text = build_pantang_warning_for_menus(menus, pantang_map)
    if warning_text: answer = answer + "\n\n" + warning_text

    primary_menu = menus[0] if menus else None
    if primary_menu is not None:
        session.add(UserMenuRiwayat(telegram_user_id=telegram_user_id,
                                    id_menu=primary_menu.id_menu,
                                    waktu=datetime.utcnow(), keterangan=text[:180]))
    return answer, primary_menu, no_context

# ============================================================
#  COMMAND HANDLER
# ============================================================
def handle_command(session: Session, telegram_user_id:int, text:str):
    lowered=text.strip().lower()

    if lowered.startswith("/start"):
        keyboard = [
            [{"text":"📖 Cara pakai","callback_data":"help_from_start"}],
            [{"text":"🎲 Rekomendasi","callback_data":"rekomendasi"}],
            [{"text":"📜 Riwayat resep","callback_data":"history"}],
            [{"text":"⚙️ Atur pantangan/alergi","callback_data":"pantang_manage"}],
            [{"text":"👀 Lihat pantangan/alergi","callback_data":"pantang_view"}],
            [{"text":"📋 Daftar menu","callback_data":"menu_list"}],
        ]
        text_start=("Halo! 👋 Aku *ChefBot*.\n"
                    "Silakan pilih tombol atau kirim nama masakan/bahan yang kamu punya.")
        return {"text": text_start, "inline_keyboard": keyboard}

    if lowered.startswith("/help"):     return get_help_text()
    if lowered.startswith("/id"):       return f"Telegram user ID kamu: `{telegram_user_id}`"
    if lowered.startswith("/history"):  return build_history_text(session, telegram_user_id)
    if lowered.startswith("/pantang"):  return handle_pantang_command(session, telegram_user_id, text)
    if lowered.startswith("/rating"):   return handle_rating_command(session, telegram_user_id, text)
    if lowered.startswith("/menu"):     return build_menu_list_text(session)

    # /addmenu (Gemini)
    if lowered.startswith("/addmenu") or lowered.startswith("/buatmenu"):
        parts=text.split(maxsplit=1)
        if GEMINI_MODEL is None:
            return "Fitur AI belum aktif. Set GEMINI_API_KEY di .env untuk memakai /addmenu."
        if len(parts)<2:
            return ("Kirim: `/addmenu <deskripsi singkat>`\nContoh: `/addmenu sapi rica rica pedas`")
        try:
            user_instr = parts[1].strip()
            # prompt → JSON via Gemini
            prompt = (
                "Buat satu resep sebagai JSON:\n"
                '{"nama_masakan":"","tingkat_kesulitan":"easy|medium|hard","bahan":[{"nama":"","jumlah":0,"satuan":""}],"langkah":["",""]}\n'
                f"Instruksi pengguna: {user_instr}"
            )
            raw = ask_gemini(prompt)
            if raw.startswith("```"): raw=raw.strip("`"); raw = raw[4:].strip() if raw.lower().startswith("json") else raw
            recipe=json.loads(raw)
            menu_obj, msg = save_generated_menu_to_db(session, recipe, source_url="gemini:/addmenu")
            if not menu_obj: return "Maaf, menu belum berhasil disimpan: "+msg
            return (f"{msg}\n\n*Ringkasan:*\n- Nama: {menu_obj.nama_masakan}\n"
                    f"- Kesulitan: {menu_obj.tingkat_kesulitan}\n\n"
                    f"Panggil: `resep {menu_obj.nama_masakan}`")
        except Exception as e:
            log.exception("/addmenu error: %s", e)
            return "Maaf, terjadi kesalahan saat membuat/menyimpan resep."

    # /addmenulink (Gemini)
    if lowered.startswith("/addmenulink") or lowered.startswith("/addmenufromlink"):
        parts=text.split(maxsplit=1)
        if GEMINI_MODEL is None:
            return "Fitur AI belum aktif. Set GEMINI_API_KEY di .env untuk memakai /addmenulink."
        if len(parts)<2: return "Kirim: `/addmenulink <URL>`"
        url = parts[1].strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return "URL tidak valid. Harus diawali http:// atau https://"
        try:
            resp = requests.get(url, timeout=20); resp.raise_for_status()
            html = resp.text[:12000]
            prompt = (
                "Ambil SATU resep utama dari HTML berikut dan kembalikan JSON:\n"
                '{"nama_masakan":"","tingkat_kesulitan":"easy|medium|hard","bahan":[{"nama":"","jumlah":0,"satuan":""}],"langkah":["",""]}\n'
                f"URL: {url}\nHTML:\n\"\"\"{html}\"\"\""
            )
            raw = ask_gemini(prompt)
            if raw.startswith("```"): raw=raw.strip("`"); raw = raw[4:].strip() if raw.lower().startswith("json") else raw
            recipe=json.loads(raw)
            menu_obj, msg = save_generated_menu_to_db(session, recipe, source_url=url)
            if not menu_obj: return "Maaf, menu belum berhasil disimpan: "+msg
            return (f"{msg}\n\n*Ringkasan dari link:*\n- Nama: {menu_obj.nama_masakan}\n"
                    f"- Kesulitan: {menu_obj.tingkat_kesulitan}\n- Sumber: {menu_obj.source_url}")
        except Exception as e:
            log.exception("/addmenulink error: %s", e)
            return "Maaf, gagal mengambil/mengekstrak resep dari link itu."

    return None  # bukan command

# ============================================================
#  CALLBACK HANDLER (INLINE BUTTON)
# ============================================================
def handle_callback_query(update: Dict[str,Any]) -> None:
    callback_id = update.get("id")
    data = update.get("data","") or ""
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    telegram_user_id = (update.get("from") or {}).get("id")

    if chat_id is None or telegram_user_id is None:
        if callback_id: answer_callback_query(callback_id); return

    if data == "history":
        with get_session() as session:
            text = build_history_text(session, telegram_user_id)
        send_message(chat_id, text); 
        if callback_id: answer_callback_query(callback_id, "Riwayat ditampilkan."); 
        return

    if data == "help_from_start":
        send_message(chat_id, get_help_text()); 
        if callback_id: answer_callback_query(callback_id); 
        return

    if data == "pantang_manage":
        send_message(chat_id,
            "*Atur pantangan / alergi*\n\n"
            "- Tambah: `/pantang tambah <nama_bahan> [pantangan|alergi]`\n"
            "- Hapus : `/pantang hapus <nama_bahan>`\n"
            "- Lihat : `/pantang list`")
        if callback_id: answer_callback_query(callback_id); 
        return

    if data == "pantang_view":
        with get_session() as session:
            text = handle_pantang_command(session, telegram_user_id, "/pantang list")
        send_message(chat_id, text); 
        if callback_id: answer_callback_query(callback_id); 
        return

    if data == "menu_list":
        with get_session() as session:
            text = build_menu_list_text(session)
        keyboard = [
            [{"text":"➕ Tambah menu (Gemini)","callback_data":"menu_add"}],
            [{"text":"➕ Tambah dari link","callback_data":"menu_add_link"}],
        ]
        send_message_with_inline_keyboard(chat_id, text, keyboard)
        if callback_id: answer_callback_query(callback_id); 
        return

    if data == "menu_add":
        send_message(chat_id, "*Tambah menu dari AI*\nKirim: `/addmenu <deskripsi>`")
        if callback_id: answer_callback_query(callback_id); 
        return

    if data == "menu_add_link":
        send_message(chat_id, "*Tambah menu dari link*\nKirim: `/addmenulink <URL>`")
        if callback_id: answer_callback_query(callback_id); 
        return

    if data == "rekomendasi":
        with get_session() as session:
            menus = get_recommendation_list(session, limit=5)
        msg, kb = build_recommendation_message(menus)
        send_message_with_inline_keyboard(chat_id, msg, kb)
        if callback_id: answer_callback_query(callback_id); 
        return

    if data.startswith("rate:"):
        parts=data.split(":")
        if len(parts)!=3:
            if callback_id: answer_callback_query(callback_id); 
            return
        _, menu_id_str, rating_str = parts
        try: id_menu=int(menu_id_str); nilai=int(rating_str)
        except ValueError:
            if callback_id: answer_callback_query(callback_id); 
            return
        if not (1<=nilai<=5):
            if callback_id: answer_callback_query(callback_id, "Rating harus 1–5.", True)
            return
        with get_session() as session:
            menu=session.get(Menu, id_menu)
            if not menu:
                msg="Menu tidak ditemukan untuk rating."
            else:
                user=ensure_user(session, telegram_user_id)
                existing=(session.query(UserMenuRating)
                          .filter(UserMenuRating.telegram_user_id==user.telegram_user_id,
                                  UserMenuRating.id_menu==id_menu).one_or_none())
                if existing:
                    existing.rating_menu=nilai; msg=f"Rating kamu untuk *{menu.nama_masakan}* diperbarui menjadi *{nilai}*."
                else:
                    session.add(UserMenuRating(telegram_user_id=user.telegram_user_id,
                                               id_menu=id_menu, rating_menu=nilai))
                    msg=f"Rating *{nilai}* untuk *{menu.nama_masakan}* disimpan."
        send_message(chat_id, msg)
        if callback_id: answer_callback_query(callback_id, "Terima kasih atas ratingnya!")
        return

    if callback_id: answer_callback_query(callback_id)

# ============================================================
#  FLASK APP
# ============================================================
app = Flask(__name__)

@app.get("/")
def index():
    return jsonify(ok=True, message="ChefBot server is running.", webhook=f"/webhook/{WEBHOOK_SECRET}")

@app.post(f"/webhook/{WEBHOOK_SECRET}")
def telegram_webhook():
    update = request.get_json(force=True, silent=True) or {}
    log.debug("Update: %s", json.dumps(update, ensure_ascii=False))

    callback_query = update.get("callback_query")
    if callback_query:
        handle_callback_query(callback_query)
        return jsonify(ok=True)

    message = update.get("message") or update.get("edited_message")
    if not message: return jsonify(ok=True)

    chat_id = message["chat"]["id"]
    from_user = message.get("from", {})
    telegram_user_id = from_user.get("id")
    text = message.get("text", "")

    if telegram_user_id is None or not isinstance(telegram_user_id, int):
        send_message(chat_id, "Maaf, aku tidak bisa mengenali user ID kamu."); return jsonify(ok=True)
    if not isinstance(text, str):
        send_message(chat_id, "Saat ini aku hanya bisa memproses pesan teks."); return jsonify(ok=True)

    text = text.strip()
    if not text:
        send_message(chat_id, "Kirimkan pesan teks ya, misalnya nama masakan atau bahan 😊")
        return jsonify(ok=True)

    send_chat_action(chat_id, "typing")

    try:
        with get_session() as session:
            ensure_user(session, telegram_user_id)

            # 1) COMMANDS
            if text.startswith("/"):
                result = handle_command(session, telegram_user_id, text)
                if result is None:
                    send_message(chat_id, "Perintah tidak dikenal. Ketik /help untuk bantuan.")
                elif isinstance(result, dict):
                    send_message_with_inline_keyboard(chat_id, result.get("text",""), result.get("inline_keyboard",[]))
                else:
                    send_message(chat_id, str(result))
                return jsonify(ok=True)

            # 2) RECOMMENDATION intent
            if is_recommendation_intent(text):
                menus = get_recommendation_list(session, limit=5)
                msg, kb = build_recommendation_message(menus)
                send_message_with_inline_keyboard(chat_id, msg, kb)
                return jsonify(ok=True)

            # 3) SMALLTALK intent (tanpa fallback)
            st_label = is_smalltalk(text)
            if st_label:
                msg, kb = smalltalk_reply(st_label)
                if kb: send_message_with_inline_keyboard(chat_id, msg, kb)
                else:  send_message(chat_id, msg)
                return jsonify(ok=True)

            # 4) GENERATE (DB + AI opsional)
            reply, primary_menu, no_context = generate_answer_for_user(session, telegram_user_id, text)
            send_message(chat_id, reply, parse_mode=None)

            # tombol lanjutan
            if primary_menu is not None:
                rating_text = (f"Kalau kamu mencoba *{primary_menu.nama_masakan}* "
                               f"(ID {primary_menu.id_menu}), beri rating masakannya:")
                keyboard = [
                    [{"text":"⭐ 1","callback_data":f"rate:{primary_menu.id_menu}:1"},
                     {"text":"⭐ 2","callback_data":f"rate:{primary_menu.id_menu}:2"},
                     {"text":"⭐ 3","callback_data":f"rate:{primary_menu.id_menu}:3"}],
                    [{"text":"⭐ 4","callback_data":f"rate:{primary_menu.id_menu}:4"},
                     {"text":"⭐ 5","callback_data":f"rate:{primary_menu.id_menu}:5"}],
                    [{"text":"📜 Riwayat saya","callback_data":"history"}],
                ]
                send_message_with_inline_keyboard(chat_id, rating_text, keyboard)
            else:
                # hanya tampilkan fallback jika memang bukan smalltalk/rekomendasi/command
                info_text=("Aku belum menemukan menu spesifik di database untuk permintaan tadi.\n\n"
                           "Coba tulis lebih jelas, misalnya:\n"
                           "- `sapi rica rica`\n- `resep nasi goreng sosis`\n- `aku punya telur, nasi, dan kecap`\n\n"
                           "Atau minta *Rekomendasi*:")
                keyboard=[[{"text":"🎲 Rekomendasi","callback_data":"rekomendasi"}],
                          [{"text":"📜 Riwayat saya","callback_data":"history"}],
                          [{"text":"📋 Daftar menu","callback_data":"menu_list"}]]
                send_message_with_inline_keyboard(chat_id, info_text, keyboard)

    except Exception as e:
        log.exception("Webhook handler error: %s", e)
        send_message(chat_id, "Maaf, terjadi kesalahan di server. Coba lagi sebentar lagi ya.")

    return jsonify(ok=True)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    log.info("Starting ChefBot server on port %s ...", port)
    app.run(host="0.0.0.0", port=port, debug=True)
