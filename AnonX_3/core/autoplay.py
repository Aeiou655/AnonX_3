# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲

import random
import re
from collections.abc import Awaitable, Callable
from logging import Logger

from AnonX_3.helpers import Track


DeepSearch = Callable[[str, int, bool, int], Awaitable[list[Track]]]


class StrictAutoplaySelector:
    INTENT_TERMS = {
        "chill": ("chill", "calm", "acoustic"),
        "party": ("party", "dance", "hit"),
        "study": ("study", "lofi", "instrumental"),
        "workout": ("workout", "gym", "energetic"),
        "myanmar": ("myanmar", "burmese", "မြန်မာ"),
        "romantic": ("love", "romantic", "အချစ်"),
    }
    ARTIST_STOPWORDS = {
        "official",
        "topic",
        "music",
        "records",
        "record",
        "channel",
        "vevo",
        "audio",
        "video",
        "lyrics",
        "lyric",
        "hd",
    }
    QUERY_STOPWORDS = {
        "official",
        "video",
        "audio",
        "lyrics",
        "lyric",
        "topic",
        "music",
        "song",
        "songs",
        "mv",
        "hd",
        "4k",
        "8k",
        "feat",
        "ft",
        "featuring",
        "version",
        "full",
        "album",
        "playlist",
        "mix",
        "best",
        "new",
        "record",
        "records",
        "channel",
        "edit",
        "the",
        "and",
        "with",
        "from",
        "original",
        "ost",
        "soundtrack",
        "promo",
        "prod",
        "by",
        "x",
        "live",
        "cover",
        "karaoke",
        "instrumental",
        "pt",
        "vol",
        "rmx",
        "vlog",
        "shorts",
        "short",
        "သီချင်း",
        "သီခ်င္း",
    }

    def __init__(self, logger: Logger):
        self.logger = logger

    def _normalize_text(self, value: str | None) -> str:
        if not isinstance(value, str):
            return ""
        text = value.casefold()
        text = re.sub(r"[\u200b-\u200d\u2060\ufeff]", "", text)
        return re.sub(r"\s+", " ", text).strip()

    def _normalize_key(self, value: str | None) -> str:
        text = self._normalize_text(value)
        if not text:
            return ""
        text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
        return re.sub(r"\s+", " ", text).strip()

    def _artist_key(self, value: str | None) -> str:
        key = self._normalize_key(value)
        if not key:
            return ""
        chunks = [part for part in key.split() if part not in self.ARTIST_STOPWORDS]
        return " ".join(chunks) or key

    def _ordered_keywords(self, value: str | None) -> list[str]:
        base = self._normalize_key(value)
        if not base:
            return []
        out: list[str] = []
        for token in base.split():
            if token in self.QUERY_STOPWORDS:
                continue
            if token.isascii() and len(token) <= 1:
                continue
            if token.isdigit():
                continue
            if token not in out:
                out.append(token)
        return out

    def _title_core_key(self, value: str | None) -> str:
        words = self._ordered_keywords(value)
        if not words:
            return self._normalize_key(value)
        return " ".join(words[:8])

    def _is_myanmar_token(self, value: str) -> bool:
        return bool(re.search(r"[\u1000-\u109f]", value or ""))

    def _extract_seed_profile(self, title: str, channel: str) -> tuple[list[str], set[str]]:
        title_words = self._ordered_keywords(title)
        artist_words = set(self._ordered_keywords(channel))
        keyword_terms = [word for word in title_words if word not in artist_words]
        if not keyword_terms:
            keyword_terms = title_words[:]
        return keyword_terms[:6], set(title_words)

    def _build_queries(
        self,
        title: str,
        artist: str,
        keyword_terms: list[str],
        intent: str = "similar",
    ) -> list[tuple[str, float]]:
        queries: list[tuple[str, float]] = []
        seen: set[str] = set()

        def add(query: str, weight: float) -> None:
            clean = (query or "").strip()
            if not clean:
                return
            key = self._normalize_key(clean)
            if not key or key in seen:
                return
            seen.add(key)
            queries.append((clean, weight))

        if title:
            add(title, 10.4)

        for term in keyword_terms[:6]:
            add(term, 10.2)
            add(f"{term} song", 10.0)
            add(f"{term} music", 9.4)
            if self._is_myanmar_token(term):
                add(f"{term} သီချင်း", 10.5)

        for idx in range(min(4, len(keyword_terms) - 1)):
            pair = f"{keyword_terms[idx]} {keyword_terms[idx + 1]}"
            add(pair, 10.7)
            add(f"{pair} song", 10.1)

        if artist and title:
            add(f"{artist} {title}", 8.0)
            add(f"{artist} {title} song", 7.6)
        for term in keyword_terms[:3]:
            if artist:
                add(f"{artist} {term}", 8.6)
        for mood_term in self.INTENT_TERMS.get(intent, ())[:2]:
            if title:
                add(f"{title} {mood_term}", 10.8)
            if keyword_terms:
                add(f"{' '.join(keyword_terms[:3])} {mood_term}", 10.6)
        return queries[:14]

    async def select(
        self,
        seed: Track,
        deep_search: DeepSearch,
        m_id: int = 0,
        exclude_ids: set[str] | None = None,
        recent_title_keys: set[str] | None = None,
        recent_artist_keys: list[str] | tuple[str, ...] | None = None,
        current_artist_streak: int = 0,
        max_same_artist_streak: int = 2,
        required_overlap_min: int = 2,
        same_artist_penalty: float = 2.2,
        repeat_artist_streak_penalty: float = 12.0,
        recent_title_penalty: float = 8.0,
        seed_exact_title_penalty: float = 9.0,
        intent: str = "similar",
    ) -> Track | None:
        if not seed:
            return None

        excluded = {x for x in (exclude_ids or set()) if x}
        if getattr(seed, "id", None):
            excluded.add(seed.id)

        title = (
            seed.title.strip()
            if isinstance(getattr(seed, "title", None), str) and seed.title.strip()
            else ""
        )
        channel = (
            seed.channel_name.strip()
            if isinstance(getattr(seed, "channel_name", None), str)
            and seed.channel_name.strip()
            else ""
        )
        if not title and not channel:
            return None

        seed_title_key = self._normalize_key(title)
        seed_title_core_key = self._title_core_key(title)
        seed_artist_key = self._artist_key(channel)
        seed_keywords, seed_title_tokens = self._extract_seed_profile(title, channel)
        default_required_overlap = 2 if len(seed_keywords) >= 2 else 1 if seed_keywords else 0
        configured_required_overlap = max(0, int(required_overlap_min))
        if configured_required_overlap > 0:
            required_overlap = min(len(seed_keywords), configured_required_overlap)
        else:
            required_overlap = default_required_overlap
        if required_overlap <= 0:
            self.logger.info(
                "Autoplay strict keyword skip: seed has no usable keywords title='%s' channel='%s'",
                title,
                channel,
            )
            return None

        clean_intent = intent if intent in self.INTENT_TERMS else "similar"
        queries = self._build_queries(title, channel, seed_keywords, clean_intent)
        if not queries:
            return None

        recent_title_norms = {self._normalize_key(x) for x in (recent_title_keys or set()) if x}
        recent_title_cores = {
            (self._title_core_key(x) or self._normalize_key(x))
            for x in (recent_title_keys or set())
            if x
        }
        recent_artists = [x for x in (recent_artist_keys or []) if x]
        recent_artist_set = set(recent_artists)
        last_artist = recent_artists[-1] if recent_artists else ""

        candidate_pool: dict[str, tuple[float, Track]] = {}
        checked = 0
        rejected_keyword = 0
        rejected_duplicates = 0
        accepted = 0
        seed_keyword_set = set(seed_keywords)

        for query, base_weight in queries:
            results = await deep_search(query, m_id, getattr(seed, "video", False), 12)
            if not results:
                continue

            for rank, item in enumerate(results):
                checked += 1
                if not item or not item.id or item.id in excluded:
                    continue
                if item.duration_sec <= 0:
                    continue

                cand_title = item.title or ""
                cand_artist = item.channel_name or ""
                cand_title_key = self._normalize_key(cand_title)
                cand_title_core_key = self._title_core_key(cand_title)
                cand_artist_key = self._artist_key(cand_artist)
                if not cand_title_key:
                    continue

                cand_title_words = set(self._ordered_keywords(cand_title))
                keyword_overlap = len(seed_keyword_set & cand_title_words)
                if keyword_overlap < required_overlap:
                    rejected_keyword += 1
                    continue
                if seed_title_core_key and cand_title_core_key == seed_title_core_key:
                    rejected_duplicates += 1
                    continue
                if cand_title_core_key and cand_title_core_key in recent_title_cores:
                    rejected_duplicates += 1
                    continue

                score = base_weight + max(0.0, 4.0 - (rank * 0.45))
                if seed_artist_key and cand_artist_key:
                    if cand_artist_key == seed_artist_key:
                        score -= same_artist_penalty
                    elif seed_artist_key in cand_artist_key or cand_artist_key in seed_artist_key:
                        score += 1.2
                score += min(8.5, keyword_overlap * 3.4)
                if seed_title_tokens and cand_title_words:
                    full_overlap = len(seed_title_tokens & cand_title_words)
                    score += min(2.0, full_overlap * 0.5)

                if cand_title_key in recent_title_norms:
                    score -= recent_title_penalty
                if seed_title_key and cand_title_key == seed_title_key:
                    score -= seed_exact_title_penalty
                if cand_artist_key and cand_artist_key in recent_artist_set:
                    score -= 1.3
                if (
                    cand_artist_key
                    and last_artist
                    and cand_artist_key == last_artist
                    and current_artist_streak >= max(1, max_same_artist_streak)
                ):
                    score -= repeat_artist_streak_penalty
                if item.duration_sec < 45:
                    score -= 1.8
                intent_terms = self.INTENT_TERMS.get(clean_intent, ())
                if intent_terms:
                    candidate_text = self._normalize_text(
                        f"{cand_title} {cand_artist}"
                    )
                    score += min(
                        2.4,
                        sum(1.2 for term in intent_terms if term in candidate_text),
                    )

                identity = (
                    f"{cand_title_core_key or cand_title_key}:{cand_artist_key}"
                    if cand_artist_key
                    else f"{cand_title_core_key or cand_title_key}:{item.id}"
                )
                prev = candidate_pool.get(identity)
                if not prev or score > prev[0]:
                    candidate_pool[identity] = (score, item)
                    accepted += 1

        if not candidate_pool:
            self.logger.info(
                "Autoplay strict keyword no-match seed='%s' keywords=%s required=%s checked=%s rejected_keyword=%s rejected_duplicates=%s accepted=0",
                title,
                seed_keywords,
                required_overlap,
                checked,
                rejected_keyword,
                rejected_duplicates,
            )
            return None

        ranked = sorted(candidate_pool.values(), key=lambda row: row[0], reverse=True)
        viable = [entry for entry in ranked[:8] if entry[0] > 0]
        if not viable:
            viable = ranked[:3]
        if not viable:
            return None

        min_score = min(row[0] for row in viable)
        weights = [(row[0] - min_score) + 1.0 for row in viable]
        selected = random.choices([row[1] for row in viable], weights=weights, k=1)[0]
        self.logger.info(
            "Autoplay strict keyword selected seed='%s' intent=%s keywords=%s required=%s checked=%s rejected_keyword=%s rejected_duplicates=%s accepted=%s selected='%s' channel='%s'",
            title,
            clean_intent,
            seed_keywords,
            required_overlap,
            checked,
            rejected_keyword,
            rejected_duplicates,
            accepted,
            getattr(selected, "title", None),
            getattr(selected, "channel_name", None),
        )
        return selected


