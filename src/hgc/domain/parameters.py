"""Registry of water-quality parameters.

This is the single source of truth mapping a USGS parameter code (or a Water Quality
Portal characteristic name) to: the PHREEQC master species it feeds, the basis the
value is reported on (as N, as P, as CaCO3...), its molar mass and its charge.

Getting this table right is the difference between a saturation index and a number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Role = Literal["cation", "anion", "neutral", "physical"]


@dataclass(frozen=True, slots=True)
class Parameter:
    pcode: str
    key: str
    label: str
    role: Role
    default_unit: str
    phreeqc: str | None = None
    basis: str | None = None
    molar_mass: float | None = None
    charge: int = 0
    aliases: tuple[str, ...] = ()

    @property
    def is_solute(self) -> bool:
        return self.phreeqc is not None

    @property
    def equivalent_weight(self) -> float | None:
        if self.molar_mass is None or self.charge == 0:
            return None
        return self.molar_mass / abs(self.charge)


PARAMETERS: tuple[Parameter, ...] = (
    # --- physical / field ----------------------------------------------------
    Parameter("00010", "temperature", "Temperature, water", "physical", "deg C",
              aliases=("Temperature, water",)),
    Parameter("00400", "ph", "pH", "physical", "std units", aliases=("pH",)),
    Parameter("00095", "conductance", "Specific conductance", "physical", "uS/cm",
              aliases=("Specific conductance",)),
    Parameter("00300", "do", "Dissolved oxygen", "neutral", "mg/l", phreeqc="O(0)",
              molar_mass=31.998, aliases=("Dissolved oxygen (DO)",)),
    Parameter("00090", "eh", "Redox potential", "physical", "mV",
              aliases=("Oxidation reduction potential (ORP)",)),
    # --- major cations -------------------------------------------------------
    Parameter("00915", "ca", "Calcium", "cation", "mg/l", phreeqc="Ca",
              molar_mass=40.078, charge=2, aliases=("Calcium",)),
    Parameter("00925", "mg", "Magnesium", "cation", "mg/l", phreeqc="Mg",
              molar_mass=24.305, charge=2, aliases=("Magnesium",)),
    Parameter("00930", "na", "Sodium", "cation", "mg/l", phreeqc="Na",
              molar_mass=22.990, charge=1, aliases=("Sodium",)),
    Parameter("00935", "k", "Potassium", "cation", "mg/l", phreeqc="K",
              molar_mass=39.098, charge=1, aliases=("Potassium",)),
    # --- major anions --------------------------------------------------------
    Parameter("00940", "cl", "Chloride", "anion", "mg/l", phreeqc="Cl",
              molar_mass=35.453, charge=-1, aliases=("Chloride",)),
    Parameter("00945", "so4", "Sulfate", "anion", "mg/l", phreeqc="S(6)",
              basis="SO4", molar_mass=96.06, charge=-2, aliases=("Sulfate",)),
    Parameter("00950", "f", "Fluoride", "anion", "mg/l", phreeqc="F",
              molar_mass=18.998, charge=-1, aliases=("Fluoride",)),
    Parameter("71870", "br", "Bromide", "anion", "mg/l", phreeqc="Br",
              molar_mass=79.904, charge=-1, aliases=("Bromide",)),
    # --- carbonate system ----------------------------------------------------
    Parameter("00440", "hco3", "Bicarbonate", "anion", "mg/l", phreeqc="Alkalinity",
              basis="HCO3", molar_mass=61.016, charge=-1, aliases=("Bicarbonate",)),
    Parameter("00410", "alk_caco3", "Alkalinity as CaCO3", "anion", "mg/l",
              phreeqc="Alkalinity", basis="CaCO3", molar_mass=50.043, charge=-1,
              aliases=("Alkalinity", "Alkalinity, total", "Acid neutralizing capacity")),
    # --- nutrients (note the reporting basis) --------------------------------
    Parameter("00618", "no3_n", "Nitrate as N", "anion", "mg/l", phreeqc="N(5)",
              basis="N", molar_mass=14.007, charge=-1, aliases=("Nitrate",)),
    Parameter("00608", "nh4_n", "Ammonia as N", "cation", "mg/l", phreeqc="N(-3)",
              basis="N", molar_mass=14.007, charge=1, aliases=("Ammonia and ammonium",)),
    Parameter("00671", "po4_p", "Orthophosphate as P", "anion", "mg/l", phreeqc="P",
              basis="P", molar_mass=30.974, charge=-2, aliases=("Orthophosphate",)),
    Parameter("00681", "doc", "Dissolved organic carbon", "neutral", "mg/l",
              aliases=("Organic carbon",)),
    # --- silica and minor / trace --------------------------------------------
    Parameter("00955", "sio2", "Silica as SiO2", "neutral", "mg/l", phreeqc="Si",
              basis="SiO2", molar_mass=60.084, aliases=("Silica",)),
    Parameter("01046", "fe", "Iron", "cation", "ug/l", phreeqc="Fe",
              molar_mass=55.845, charge=2, aliases=("Iron",)),
    Parameter("01056", "mn", "Manganese", "cation", "ug/l", phreeqc="Mn",
              molar_mass=54.938, charge=2, aliases=("Manganese",)),
    Parameter("01106", "al", "Aluminum", "cation", "ug/l", phreeqc="Al",
              molar_mass=26.982, charge=3, aliases=("Aluminum",)),
    Parameter("01080", "sr", "Strontium", "cation", "ug/l", phreeqc="Sr",
              molar_mass=87.62, charge=2, aliases=("Strontium",)),
    Parameter("01005", "ba", "Barium", "cation", "ug/l", phreeqc="Ba",
              molar_mass=137.327, charge=2, aliases=("Barium",)),
    Parameter("01130", "li", "Lithium", "cation", "ug/l", phreeqc="Li",
              molar_mass=6.941, charge=1, aliases=("Lithium",)),
    Parameter("01020", "b", "Boron", "neutral", "ug/l", phreeqc="B",
              molar_mass=10.811, aliases=("Boron",)),
    Parameter("01000", "as", "Arsenic", "neutral", "ug/l", phreeqc="As",
              molar_mass=74.922, aliases=("Arsenic",)),
)

BY_PCODE: dict[str, Parameter] = {p.pcode: p for p in PARAMETERS}
BY_KEY: dict[str, Parameter] = {p.key: p for p in PARAMETERS}
_BY_ALIAS: dict[str, Parameter] = {
    alias.casefold(): p for p in PARAMETERS for alias in (*p.aliases, p.label, p.key)
}

#: Parameters that must be present for a speciation model to be worth running.
REQUIRED_FOR_SPECIATION: tuple[str, ...] = ("ph", "ca", "mg", "na", "cl", "so4")

#: Phases offered by default in the UI; every one is present in phreeqc.dat.
DEFAULT_PHASES: tuple[str, ...] = (
    "Calcite", "Dolomite", "Gypsum", "Anhydrite", "Aragonite", "Siderite",
    "Fluorite", "Halite", "Quartz", "Chalcedony", "SiO2(a)", "CO2(g)", "O2(g)",
)


def lookup(token: str) -> Parameter | None:
    """Resolve a pcode, canonical key, label, or WQP characteristic name."""
    token = token.strip()
    if token in BY_PCODE:
        return BY_PCODE[token]
    return _BY_ALIAS.get(token.casefold())


def solutes() -> tuple[Parameter, ...]:
    return tuple(p for p in PARAMETERS if p.is_solute)
