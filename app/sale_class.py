"""OTC vs prescription label for a drug definition.

Existing catalog rows are labeled once. A later edit in Drug Catalog is kept.
"""
import re

_RX_GENERICS = {
    "amoxicillin",
    "azithromycin",
    "cefalexin",
    "cefixime",
    "cefuroxime",
    "ciprofloxacin",
    "clarithromycin",
    "clindamycin",
    "cloxacillin",
    "co-amoxiclav",
    "cotrimoxazole",
    "doxycycline",
    "erythromycin",
    "metronidazole",
    "nitrofurantoin",
    "sultamicillin",
    "sultamicillin tosylate",
    "mupirocin",
    "amlodipine",
    "captopril",
    "carvedilol",
    "clonidine",
    "clonidine hcl",
    "irbesartan",
    "losartan",
    "metoprolol",
    "telmisartan",
    "atorvastatin",
    "rosuvastatin",
    "simvastatin",
    "clopidogrel",
    "isdn",
    "trimetazidine",
    "trimetazidine hcl",
    "gliclazide",
    "metformin",
    "colchicine",
    "febuxostat",
    "methylprednisolone",
    "prednisone",
    "levetiracetam",
    "eperisone",
    "diclofenac",
    "celecoxib",
    "tramadol",
    "tramadol hcl",
    "metoclopramide",
    "metoclopramide hcl",
    "montelukast + levocetirizine hcl",
    "sevelamer carbonate",
    "tamsulosin",
    "tamsulosin hcl",
    "pantoprazole",
    "ranitidine",
    "nystatin",
    "dicycloverine",
    "salbutamol",
    "tranexamic acid",
    "orphenadrine citrate + paracetamol",
}

_OTC_GENERICS = {
    "acetylcysteine",
    "ambroxol",
    "ascorbic acid",
    "aspirin",
    "butamirate citrate",
    "calcium carbonate + vit d3",
    "carbocisteine",
    "cetirizine",
    "chlorphenamine maleate",
    "diphenhydramine",
    "ibuprofen",
    "ibuprofen + paracetamol",
    "loperamide",
    "loratadine",
    "mefenamic acid",
    "multivitamins",
    "multivitamins with lysine, taurine, zinc, cgf",
    "ferrous sulfate",
    "oral rehydration salts",
    "vitex negundo l. lagundi leaf",
    "zinc",
    "vitamin b-complex",
    "almg",
    "almgoh",
    "famotidine + calcium carbonate + magnesium hydroxide",
    "lactulose",
    "mebendazole",
    "domperidone",
    "hnbb",
    "dextromethorphan hbr + guaifenesin",
    "dextromethorphan hbr + phenylpropanolamine hcl + paracetamol",
    "guaifenesin + dextromethorphan hbr + phenylpropanolamine hcl + chlorphenamine maleate",
    "guaifenesin + phenylpropanolamine hcl + chlorphenamine maleate",
    "paracetamol",
    "paracetamol + caffeine",
    "paracetamol + guaifenesin + phenylpropanolamine hcl + dextromethorphan hbr + chlorphenamine maleate",
    "paracetamol + phenylphrine hcl + dextromethorphan hbr",
    "paracetamol + phenylpropanolamine hcl + chlorphenamine maleate",
    "paracetamol + prophenazone + caffeine",
    "phenylphrine hcl + chlorphenamine maleate + paracetamol",
    "phenylphrine hcl + paracetamol",
    "phenylpropanolamine",
    "phenylpropanolamine hcl + chlorphenamine maleate + paracetamol",
    "phenylpropanolamine hcl + paracetamol",
    "salbutamol + guaifenesin",
}

_RX_CATEGORIES = {
    "antibiotic",
    "topical antibiotic",
    "antihypertensive",
    "antihyperlipidemic",
    "antidiabetic",
    "antigout",
    "corticosteroid",
    "anticonvulsant",
    "cardiovascular",
    "urologic",
    "phosphate binder",
    "muscle relaxant",
    "hemostatic",
    "bronchodilator",
}

_OTC_CATEGORIES = {
    "vitamin",
    "supplement",
    "vitamin/supplement",
    "analgesic/antipyretic",
    "cough and cold",
    "antacid/ppi",
    "antihistamine",
    "antidiarrheal",
    "electrolyte",
    "laxative",
    "nsaid",
    "analgesic/nsaid",
    "anthelmintic",
    "antiemetic",
    "antispasmodic",
}

_TYPOS = {
    "diphenydramine": "diphenhydramine",
    "doxcycline": "doxycycline",
    "methylprednisone": "methylprednisolone",
}


def _norm(value: str) -> str:
    text = (value or "").lower().strip().replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text)


def classify_sale_class(generic_name: str, category: str = "", dosage: str = "") -> str:
    """Return 'otc' or 'rx'. Unknown medicines stay rx."""
    generic = _TYPOS.get(_norm(generic_name), _norm(generic_name))
    if "tramadol" in generic:
        return "rx"
    if generic == "omeprazole":
        dose = _norm(dosage)
        return "otc" if dose.startswith("20") else "rx"
    if generic in _RX_GENERICS:
        return "rx"
    if generic in _OTC_GENERICS:
        return "otc"
    cat = _norm(category)
    if cat in _RX_CATEGORIES:
        return "rx"
    if cat in _OTC_CATEGORIES:
        return "otc"
    return "rx"
