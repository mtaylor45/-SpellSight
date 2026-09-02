"""The spell catalog.

Ids here become MQTT topic segments and Home Assistant entity ids, so they are
part of the frozen interface in SPEC.md section 4. Renaming an id orphans a
retained discovery config and silently breaks a user's automations — add new
spells freely, rename existing ones never.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Spell:
    """A castable spell.

    `effect` is descriptive only. This device deliberately owns no show logic;
    what a spell actually does lives in Home Assistant automations.
    """

    id: str
    name: str
    effect: str


SPELLS: tuple[Spell, ...] = (
    Spell("lumos", "Lumos", "Light"),
    Spell("nox", "Nox", "Extinguish light"),
    Spell("incendio", "Incendio", "Conjure fire"),
    Spell("aguamenti", "Aguamenti", "Conjure water"),
    Spell("glacius", "Glacius", "Freeze to ice"),
    Spell("ventus", "Ventus", "Gust of wind"),
    Spell("alohomora", "Alohomora", "Unlock"),
    Spell("colloportus", "Colloportus", "Lock"),
    Spell("accio", "Accio", "Summon"),
    Spell("depulso", "Depulso", "Banish"),
    Spell("wingardium_leviosa", "Wingardium Leviosa", "Levitate"),
    Spell("locomotor", "Locomotor", "Move an object"),
    Spell("arresto_momentum", "Arresto Momentum", "Slow a falling object"),
    Spell("expelliarmus", "Expelliarmus", "Disarm"),
    Spell("protego", "Protego", "Shield"),
    Spell("stupefy", "Stupefy", "Stun"),
    Spell("rennervate", "Rennervate", "Revive"),
    Spell("immobulus", "Immobulus", "Freeze in place"),
    Spell("petrificus_totalus", "Petrificus Totalus", "Full body-bind"),
    Spell("reparo", "Reparo", "Repair"),
    Spell("reducto", "Reducto", "Blast apart"),
    Spell("bombarda", "Bombarda", "Explode"),
    Spell("confringo", "Confringo", "Blasting curse"),
    Spell("diffindo", "Diffindo", "Sever"),
    Spell("evanesco", "Evanesco", "Vanish"),
    Spell("engorgio", "Engorgio", "Enlarge"),
    Spell("reducio", "Reducio", "Shrink"),
    Spell("silencio", "Silencio", "Silence"),
    Spell("sonorus", "Sonorus", "Amplify the voice"),
    Spell("quietus", "Quietus", "Quieten the voice"),
    Spell("muffliato", "Muffliato", "Fill nearby ears with buzzing"),
    Spell("expecto_patronum", "Expecto Patronum", "Conjure a Patronus"),
    Spell("riddikulus", "Riddikulus", "Defeat a Boggart"),
    Spell("obliviate", "Obliviate", "Erase memory"),
    Spell("legilimens", "Legilimens", "Read thoughts"),
    Spell("imperio", "Imperio", "Control another"),
    Spell("crucio", "Crucio", "Inflict pain"),
    Spell("avada_kedavra", "Avada Kedavra", "The Killing Curse"),
    Spell("sectumsempra", "Sectumsempra", "Slash"),
    Spell("episkey", "Episkey", "Heal a minor injury"),
    Spell("ferula", "Ferula", "Bandage and splint"),
    Spell("scourgify", "Scourgify", "Clean"),
    Spell("tergeo", "Tergeo", "Siphon away"),
    Spell("revelio", "Revelio", "Reveal what is hidden"),
    Spell("finite_incantatem", "Finite Incantatem", "End spell effects"),
)

SPELLS_BY_ID: dict[str, Spell] = {spell.id: spell for spell in SPELLS}


def get_spell(spell_id: str) -> Spell | None:
    """Look up a spell by id, or None if it isn't in the catalog."""
    return SPELLS_BY_ID.get(spell_id)


def spell_name(spell_id: str) -> str:
    """Display name for a spell id, falling back to the id itself."""
    spell = SPELLS_BY_ID.get(spell_id)
    return spell.name if spell else spell_id
