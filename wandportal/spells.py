"""Canonical spell catalog (film-canon incantations).

Each entry becomes a Home Assistant entity when enabled in config.yaml.
`suggested` is only a hint shown in the training console — the actual
automation lives in Home Assistant, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Spell:
    id: str
    name: str
    effect: str
    suggested: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# Ordered roughly by how useful they are as home-automation triggers.
CATALOG: list[Spell] = [
    Spell("lumos", "Lumos", "Creates light at the wand tip", "Turn lights on"),
    Spell("nox", "Nox", "Extinguishes wand light", "Turn lights off"),
    Spell("lumos_maxima", "Lumos Maxima", "Blinding burst of light", "All lights to 100%"),
    Spell("alohomora", "Alohomora", "Unlocks doors", "Unlock smart lock"),
    Spell("colloportus", "Colloportus", "Seals a door shut", "Lock smart lock"),
    Spell("incendio", "Incendio", "Conjures fire", "Fireplace / warm scene"),
    Spell("aguamenti", "Aguamenti", "Jet of water", "Irrigation or humidifier"),
    Spell("accio", "Accio", "Summons an object", "Robot vacuum: come home"),
    Spell("wingardium_leviosa", "Wingardium Leviosa", "Levitates an object", "Raise blinds / desk"),
    Spell("descendo", "Descendo", "Moves an object downward", "Lower blinds / projector screen"),
    Spell("silencio", "Silencio", "Silences a target", "Mute media players"),
    Spell("sonorus", "Sonorus", "Amplifies the voice", "Volume up"),
    Spell("quietus", "Quietus", "Counters Sonorus", "Volume down"),
    Spell("expelliarmus", "Expelliarmus", "Disarms an opponent", "Kill switch for a scene"),
    Spell("stupefy", "Stupefy", "Stuns a target", "Pause all media"),
    Spell("rennervate", "Rennervate", "Revives a stunned target", "Resume media"),
    Spell("protego", "Protego", "Shield charm", "Arm alarm system"),
    Spell("finite_incantatem", "Finite Incantatem", "Ends ongoing spell effects", "Disarm alarm / reset scene"),
    Spell("expecto_patronum", "Expecto Patronum", "Conjures a Patronus", "Night light + calm scene"),
    Spell("reparo", "Reparo", "Repairs broken objects", "Restart a service or device"),
    Spell("scourgify", "Scourgify", "Cleans a surface", "Start robot vacuum"),
    Spell("tergeo", "Tergeo", "Siphons mess away", "Vacuum spot clean"),
    Spell("revelio", "Revelio", "Reveals hidden things", "Show camera feeds on a display"),
    Spell("homenum_revelio", "Homenum Revelio", "Reveals human presence", "Announce who is home"),
    Spell("point_me", "Point Me", "Four-point compass spell", "Announce weather / status"),
    Spell("obscuro", "Obscuro", "Blindfolds a target", "Privacy mode: cameras off"),
    Spell("impervius", "Impervius", "Repels water", "Close windows / awning"),
    Spell("immobulus", "Immobulus", "Freezes targets in place", "Freeze all automations"),
    Spell("reducto", "Reducto", "Blasts objects apart", "Panic / all off"),
    Spell("riddikulus", "Riddikulus", "Defeats a Boggart", "Play a joke sound"),
    Spell("morsmordre", "Morsmordre", "Conjures the Dark Mark", "Halloween scene"),
    Spell("confringo", "Confringo", "Explosive blasting curse", "Strobe effect"),
    Spell("diffindo", "Diffindo", "Severing charm", "Cut power to a switch"),
    Spell("engorgio", "Engorgio", "Enlarges an object", "Brightness up"),
    Spell("reducio", "Reducio", "Shrinks an object", "Brightness down"),
    Spell("locomotor", "Locomotor", "Moves an object", "Open garage door"),
    Spell("portus", "Portus", "Creates a Portkey", "Trigger a scene shortcut"),
    Spell("geminio", "Geminio", "Duplicates an object", "Mirror a scene to another room"),
    Spell("muffliato", "Muffliato", "Fills ears with buzzing", "White noise on"),
    Spell("episkey", "Episkey", "Heals minor injuries", "Reset devices to default state"),
    Spell("serpensortia", "Serpensortia", "Conjures a snake", "Green scene"),
    Spell("tarantallegra", "Tarantallegra", "Forces the target to dance", "Party scene + music"),
    Spell("petrificus_totalus", "Petrificus Totalus", "Full body-bind", "Do not disturb mode"),
    Spell("obliviate", "Obliviate", "Erases memories", "Clear notifications / reset history"),
    Spell("avada_kedavra", "Avada Kedavra", "The Killing Curse", "Master off — everything"),
]

BY_ID: dict[str, Spell] = {s.id: s for s in CATALOG}


def get(spell_id: str) -> Spell | None:
    return BY_ID.get(spell_id)


def resolve(ids: list[str]) -> list[Spell]:
    """Return catalog entries for the given ids, preserving config order."""
    out = []
    for sid in ids:
        spell = BY_ID.get(sid)
        if spell is None:
            raise ValueError(
                f"Unknown spell id {sid!r}. Valid ids: {', '.join(sorted(BY_ID))}"
            )
        out.append(spell)
    return out
