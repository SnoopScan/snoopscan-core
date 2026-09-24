"""Geographic coherence (06-proxy-layer.md section 7).

Fingerprint coherence extends to the network layer: a German exit IP with a US
timezone and English-only language headers is a detectable anomaly.

Everything derives from a single country code here rather than being assembled
at each call site — assembling it per call site is how the three drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.core.models import Location


@dataclass(frozen=True)
class GeoProfile:
    country: str
    languages: tuple[str, ...]
    timezone: str

    @property
    def locale(self) -> str:
        return self.languages[0]

    @property
    def accept_language(self) -> str:
        """Weighted Accept-Language, primary first then a base-language fallback."""
        parts = [self.languages[0]]
        base = self.languages[0].split("-")[0]
        if base != self.languages[0]:
            parts.append(f"{base};q=0.9")
        for extra in self.languages[1:]:
            parts.append(f"{extra};q=0.8")
        return ",".join(parts)


# Country code -> (languages, IANA timezone). Extend as proxy geography grows;
# every entry must be internally consistent.
_PROFILES: dict[str, tuple[tuple[str, ...], str]] = {
    "GB": (("en-GB",), "Europe/London"),
    "IE": (("en-IE", "en-GB"), "Europe/Dublin"),
    "US": (("en-US",), "America/New_York"),
    "CA": (("en-CA", "fr-CA"), "America/Toronto"),
    "AU": (("en-AU",), "Australia/Sydney"),
    "NZ": (("en-NZ",), "Pacific/Auckland"),
    "DE": (("de-DE",), "Europe/Berlin"),
    "AT": (("de-AT", "de-DE"), "Europe/Vienna"),
    "CH": (("de-CH", "fr-CH"), "Europe/Zurich"),
    "FR": (("fr-FR",), "Europe/Paris"),
    "BE": (("nl-BE", "fr-BE"), "Europe/Brussels"),
    "NL": (("nl-NL",), "Europe/Amsterdam"),
    "ES": (("es-ES",), "Europe/Madrid"),
    "IT": (("it-IT",), "Europe/Rome"),
    "PT": (("pt-PT",), "Europe/Lisbon"),
    "PL": (("pl-PL",), "Europe/Warsaw"),
    "SE": (("sv-SE",), "Europe/Stockholm"),
    "NO": (("nb-NO",), "Europe/Oslo"),
    "DK": (("da-DK",), "Europe/Copenhagen"),
    "FI": (("fi-FI",), "Europe/Helsinki"),
    "CZ": (("cs-CZ",), "Europe/Prague"),
    "GR": (("el-GR",), "Europe/Athens"),
    "BR": (("pt-BR",), "America/Sao_Paulo"),
    "MX": (("es-MX",), "America/Mexico_City"),
    "AR": (("es-AR",), "America/Argentina/Buenos_Aires"),
    "JP": (("ja-JP",), "Asia/Tokyo"),
    "KR": (("ko-KR",), "Asia/Seoul"),
    "CN": (("zh-CN",), "Asia/Shanghai"),
    "HK": (("zh-HK", "en-GB"), "Asia/Hong_Kong"),
    "SG": (("en-SG",), "Asia/Singapore"),
    "IN": (("en-IN", "hi-IN"), "Asia/Kolkata"),
    "ZA": (("en-ZA",), "Africa/Johannesburg"),
    "AE": (("ar-AE", "en-GB"), "Asia/Dubai"),
    "IL": (("he-IL", "en-GB"), "Asia/Jerusalem"),
    "TR": (("tr-TR",), "Europe/Istanbul"),
}

# Used when a country is unknown. Neutral rather than wrong.
_FALLBACK = GeoProfile(country="US", languages=("en-US",), timezone="America/New_York")


def profile_for(country: str | None) -> GeoProfile:
    """The full coherent set for a country code."""
    if not country:
        return _FALLBACK
    code = country.upper()
    entry = _PROFILES.get(code)
    if entry is None:
        return GeoProfile(country=code, languages=_FALLBACK.languages, timezone=_FALLBACK.timezone)
    languages, timezone = entry
    return GeoProfile(country=code, languages=languages, timezone=timezone)


def resolve_location(location: Location | None) -> GeoProfile:
    """Derive the coherent profile, honouring caller-supplied languages.

    Setting `country` without `languages` is the common case: languages are
    derived rather than left mismatched.
    """
    if location is None:
        return _FALLBACK
    base = profile_for(location.country)
    if location.languages:
        return GeoProfile(
            country=base.country,
            languages=tuple(location.languages),
            timezone=base.timezone,
        )
    return base


def accept_language_for(location: Location | None) -> str | None:
    if location is None:
        return None
    return resolve_location(location).accept_language


def is_supported(country: str) -> bool:
    return country.upper() in _PROFILES


def supported_countries() -> tuple[str, ...]:
    return tuple(sorted(_PROFILES))
