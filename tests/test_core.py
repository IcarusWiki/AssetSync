import io

from PIL import Image

from assetsync import ledger as L
from assetsync.filepage import (
    ensure_category,
    has_category,
    mark_historical,
    new_file_page,
    redirect_page,
    redirect_target,
)
from assetsync.manifest import texture_hashes
from assetsync.pixels import pixel_hash
from assetsync.redirects import display_name, icon_texture, plan_redirects, wanted_redirects

GUN = "Assets/2DArt/UI/Items/Item_Icons/Weapons/Guns/ITEM_Pistol_Handgun"


def png(pixels, size=(2, 1)):
    im = Image.new("RGBA", size)
    im.putdata(pixels)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# --- pixels ------------------------------------------------------------------------------

def test_pixel_hash_ignores_colour_under_transparent_pixels():
    a = png([(255, 0, 0, 255), (10, 20, 30, 0)])
    b = png([(255, 0, 0, 255), (0, 0, 0, 0)])
    assert pixel_hash(a) == pixel_hash(b)


def test_pixel_hash_sees_visible_changes():
    a = png([(255, 0, 0, 255), (0, 0, 0, 0)])
    b = png([(255, 0, 0, 254), (0, 0, 0, 0)])
    assert pixel_hash(a) != pixel_hash(b)


def test_pixel_hash_includes_size():
    assert pixel_hash(png([(0, 0, 0, 0)] * 2, (2, 1))) != pixel_hash(png([(0, 0, 0, 0)] * 2, (1, 2)))


# --- manifest ----------------------------------------------------------------------------

def test_texture_hashes_combine_parts_and_skip_orphans():
    files = {f"{GUN}.uasset": "aa", f"{GUN}.uexp": "bb", "Assets/2DArt/UI/x.ubulk": "cc"}
    combined = texture_hashes(files)
    assert list(combined) == [GUN]
    files[f"{GUN}.uexp"] = "bc"
    assert texture_hashes(files)[GUN] != combined[GUN]


# --- ledger ------------------------------------------------------------------------------

def synced(pak, pixels, wiki=None):
    return L.TextureRecord(pak_sha1=pak, pixel_sha256=pixels, wiki_file=GUN.rsplit("/", 1)[1] + ".png",
                           wiki_pixel_sha256=wiki or pixels, status=L.SYNCED)


def test_recook_without_pixel_change_is_not_uploaded():
    ledger = L.Ledger(textures={GUN: synced("p1", "x1")})
    scanned = {GUN: "p2"}
    changes = L.diff(ledger, scanned)
    assert changes.changed == [GUN]
    plan = L.plan_update(ledger, scanned, changes, {GUN: "x1"}, build="200", version="V3.0.27.1")
    assert plan["uploads"] == [] and plan["hash_only"] == [GUN]
    assert ledger.textures[GUN].pak_sha1 == "p2"


def test_pixel_change_is_uploaded_and_retried_until_published():
    ledger = L.Ledger(textures={GUN: synced("p1", "x1")})
    scanned = {GUN: "p2"}
    plan = L.plan_update(ledger, scanned, L.diff(ledger, scanned), {GUN: "x2"}, build="200", version=None)
    assert [u["wiki_file"] for u in plan["uploads"]] == ["ITEM_Pistol_Handgun.png"]
    assert plan["uploads"][0]["category"] == "Assets/2DArt/UI/Items/Item_Icons/Weapons/Guns"
    # Nothing changed in the game since, but the wiki still has the old pixels.
    again = L.plan_update(ledger, scanned, L.diff(ledger, scanned), {}, build="201", version=None)
    assert len(again["uploads"]) == 1


def test_new_texture_goes_to_review_and_removed_to_historical():
    other = "Assets/2DArt/UI/Icons/ICON_New"
    ledger = L.Ledger(textures={GUN: synced("p1", "x1")})
    scanned = {other: "q1"}
    changes = L.diff(ledger, scanned)
    plan = L.plan_update(ledger, scanned, changes, {other: "y1"}, build="200", version="V3.0.27.1")
    assert [r["texture"] for r in plan["review"]] == [other]
    assert [h["texture"] for h in plan["historical"]] == [GUN]
    assert ledger.textures[GUN].status == L.REMOVED and ledger.textures[GUN].removed_in == "V3.0.27.1"
    assert plan["uploads"] == []


def test_ledger_round_trip(tmp_path):
    ledger = L.Ledger(textures={GUN: synced("p1", "x1")}, redirects={"ITEM_A.png": "ITEM_B.png"})
    ledger.save(tmp_path)
    assert L.Ledger.load(tmp_path) == ledger


# --- redirects ---------------------------------------------------------------------------

ITEMABLE = {
    "Defaults": {"DisplayName": "", "Icon": "None"},
    "Rows": [
        {"Name": "Item_Pistol_Handgun",
         "DisplayName": 'NSLOCTEXT("D_Itemable", "Item_Pistol_Handgun-DisplayName", "One-Shot Pistol")',
         "Icon": f"/Game/{GUN}.ITEM_Pistol_Handgun"},
        {"Name": "Item_Crossbow",
         "DisplayName": 'NSLOCTEXT("D_Itemable", "k", "Inaris \\"Rapid\\" Crossbow")',
         "Icon": "/Game/Assets/2DArt/UI/Items/Item_Icons/ITEM_Crossbow.ITEM_Crossbow"},
        {"Name": "Dev", "DisplayName": 'INVTEXT("[DNT] Test Battery")',
         "Icon": "/Game/Assets/2DArt/UI/Items/ITEM_Battery.ITEM_Battery"},
        {"Name": "NoIcon", "DisplayName": 'NSLOCTEXT("D_Itemable", "k", "Nothing")'},
        {"Name": "Same", "DisplayName": 'NSLOCTEXT("D_Itemable", "k", "Fibre")',
         "Icon": "/Game/Assets/2DArt/UI/Items/ITEM_Fibre.ITEM_Fibre"},
        {"Name": "Clash", "DisplayName": 'NSLOCTEXT("D_Itemable", "k", "One-Shot Pistol")',
         "Icon": "/Game/Assets/2DArt/UI/Items/ITEM_Other.ITEM_Other"},
    ],
}


def test_display_name_parsing():
    assert display_name('NSLOCTEXT("a", "b", "Xigo \\"Jack\\" Axe")') == 'Xigo "Jack" Axe'
    assert display_name('INVTEXT("[DNT] Meta Shovel")') is None
    assert icon_texture("/Game/Developers/x/UI/Icons/T.T") is None
    assert icon_texture(f"/Game/{GUN}.ITEM_Pistol_Handgun") == GUN


def test_wanted_redirects_skip_identity_and_report_conflicts():
    found = wanted_redirects(ITEMABLE)
    assert found.wanted == {'ITEM_Inaris "Rapid" Crossbow.png': "ITEM_Crossbow.png"}
    assert found.conflicts == {"ITEM_One-Shot Pistol.png": ["ITEM_Other.png", "ITEM_Pistol_Handgun.png"]}


ACH = "Assets/2DArt/UI/Accolades/Achievements/T_ACH_1_Survival101_Complete"
ACCOLADES = {
    "Defaults": {"DisplayName": "", "Icon": "None", "SteamAchievementId": "None"},
    "Rows": [
        {"Name": "PickupBasicResource",
         "DisplayName": 'NSLOCTEXT("D_Accolades", "k", "Survival 101")',
         "Icon": f"/Game/{ACH}.T_ACH_1_Survival101_Complete",
         "SteamAchievementId": "ACH_PickupBasicResource"},
        {"Name": "ForestScanComplete",
         "DisplayName": 'NSLOCTEXT("D_Accolades", "k", "Forest Scanner")',
         "Icon": "/Game/Assets/2DArt/UI/Accolades/Ribbons/ribbon-forestscanner.ribbon-forestscanner"},
        {"Name": "NotUploaded",
         "DisplayName": 'NSLOCTEXT("D_Accolades", "k", "Brand New")',
         "Icon": "/Game/Assets/2DArt/UI/Accolades/Achievements/T_ACH_New.T_ACH_New",
         "SteamAchievementId": "ACH_New"},
    ],
}


def test_achievement_redirects_only_steam_achievements_with_synced_targets():
    from assetsync.redirects import achievement_redirects

    found = achievement_redirects(ACCOLADES, synced={ACH})
    assert found.wanted == {"ACH_Survival 101.png": "T_ACH_1_Survival101_Complete.png"}
    # The ribbon is not an achievement; the new achievement's icon isn't on the wiki yet.
    assert found.review == {"ACH_Brand New.png": ["T_ACH_New.png"]}


def _loc(text):
    return f'NSLOCTEXT("t", "k", "{text}")'


def _creature_tables():
    ui = "Assets/2DArt/UI"

    def icon(path):
        return f"/Game/{ui}/{path}.{path.rsplit('/', 1)[-1]}"

    heads = {  # trophy row -> (vestige display name, icon)
        "Wolf_Head": ("Wolf Vestige", "Items/ITEM_Wolf_Head"),
        "Alphawolf_Head": ("Black Wolf Vestige", "Items/ITEM_AlphaWolf_Head"),
        "Boar_Head": ("Wild Boar Vestige", "Items/T_ITEM_WildBoar_Head"),
        "Hen_Head": ("Red Chicken Vestige", "Items/T_ITEM_Chicken_Head"),
        "Rooster_Head": ("Rooster Vestige", "Items/T_ITEM_Rooster_Head"),
        "New_Head": ("Newbie Vestige", "Items/T_ITEM_New_Head"),
    }
    bestiary = [  # row, creature name, bestiary image, AI setups (trophy rows)
        ("Forest_Wolf", "Wolf", "T_Bestiary_Wolf", ["Wolf_Head", "Wolf_Head"]),
        ("Alpha_Wolf_Boss", "Black Wolf", "T_Bestiary_AlphaWolf", ["AlphaWolf_Head", "Wolf_Head"]),
        ("Wild_Boar", "Boar", "T_Bestiary_WildBoar", ["Boar_Head"]),
        ("Chicken", "Chicken", "T_Bestiary_Chickens", ["Hen_Head", "Rooster_Head"]),
        ("Bee", "Bee", "T_Bestiary_Bees", []),
        ("Newbie", "Newbie", "T_Bestiary_New", ["New_Head"]),
    ]
    return {
        "Traits/D_Itemable": {"Defaults": {"Icon": "None"}, "Rows": [
            {"Name": f"Item_{row}", "DisplayName": _loc(name), "Icon": icon(path)}
            for row, (name, path) in heads.items()]},
        "Items/D_ItemsStatic": {"Rows": [
            {"Name": row, "Itemable": {"RowName": f"Item_{row}"}} for row in heads]},
        "Items/D_ItemTemplate": {"Rows": [
            {"Name": row, "ItemStaticData": {"RowName": row}} for row in heads]},
        "Items/D_ItemRewards": {"Rows": [
            {"Name": row, "Rewards": [{"Item": {"RowName": row}}]} for row in heads]},
        "Bestiary/D_BestiaryData": {"Defaults": {"Image": "None"}, "Rows": [
            {"Name": row, "CreatureName": _loc(name), "Image": icon(f"FieldGuide/Creatures/{img}")}
            for row, name, img, _ in bestiary]},
        "AI/D_AISetup": {"Rows": [
            {"Name": f"{row}_{i}", "BestiaryGroup": {"RowName": row}, "Trophy": {"RowName": trophy}}
            for row, _, _, trophies in bestiary for i, trophy in enumerate(trophies)]},
    }


def test_creature_redirects_prefer_vestige_then_bestiary_image():
    from assetsync.redirects import creature_redirects

    tables = _creature_tables()
    synced = {
        f"Assets/2DArt/UI/{p}" for p in (
            "Items/ITEM_Wolf_Head", "Items/ITEM_AlphaWolf_Head", "Items/T_ITEM_WildBoar_Head",
            "Items/T_ITEM_Chicken_Head", "Items/T_ITEM_Rooster_Head",
            "FieldGuide/Creatures/T_Bestiary_Wolf", "FieldGuide/Creatures/T_Bestiary_Chickens",
            "FieldGuide/Creatures/T_Bestiary_Bees", "FieldGuide/Creatures/T_Bestiary_New",
        )
    }
    found = creature_redirects(tables, synced)
    assert found.wanted == {
        "CRE_Wolf.png": "ITEM_Wolf_Head.png",                # exact "<name> Vestige"
        "CRE_Black Wolf.png": "ITEM_AlphaWolf_Head.png",     # exact name beats the mixed trophy chain
        "CRE_Boar.png": "T_ITEM_WildBoar_Head.png",          # only vestige the group drops (case-insensitive rows)
        "CRE_Chicken.png": "T_Bestiary_Chickens.png",        # several vestiges: basic redirect...
        "CRE_Bee.png": "T_Bestiary_Bees.png",                # no vestige at all
        "CRE_Newbie.png": "T_Bestiary_New.png",              # vestige not on the wiki yet
    }
    # ...and the ambiguous group goes to review for a Module:Link icon override.
    assert found.review == {"CRE_Chicken.png": ["T_ITEM_Chicken_Head.png", "T_ITEM_Rooster_Head.png"]}


def test_review_is_reported_once_unless_candidates_change():
    from assetsync.redirects import new_reviews

    review = {"ACH_A.png": ["T_A.png"], "CRE_B.png": ["X.png", "Y.png"]}
    reported = {"ACH_A.png": ["T_A.png"], "CRE_B.png": ["X.png"]}
    assert new_reviews(review, reported) == {"CRE_B.png": ["X.png", "Y.png"]}


def test_plan_redirects_keeps_old_names_and_matches_mediawiki_titles():
    known = {"ITEM_One-Shot_Pistol.png": "ITEM_Pistol_Handgun.png", "ITEM_Old Name.png": "ITEM_X.png"}
    wanted = {"ITEM_One-Shot Pistol.png": "ITEM_Pistol_Handgun.png", "ITEM_New Name.png": "ITEM_X.png",
              "ITEM_Moved.png": "ITEM_Y.png"}
    known["ITEM_Moved.png"] = "ITEM_Z.png"
    plan = plan_redirects(known, wanted)
    assert plan["create"] == [{"redirect": "ITEM_New Name.png", "target": "ITEM_X.png"}]
    assert plan["retarget"] == [{"redirect": "ITEM_Moved.png", "target": "ITEM_Y.png", "previous": "ITEM_Z.png"}]


# --- file pages --------------------------------------------------------------------------

def test_file_page_text_and_categories():
    text = new_file_page("Assets/2DArt/UI/Items/Item_Icons/Weapons/Guns")
    assert text == ("== Licensing ==\n{{License|game}}\n\n"
                    "[[Category:Assets/2DArt/UI/Items/Item_Icons/Weapons/Guns]]")
    assert has_category("[[Category:Assets/2DArt/UI/Items/Item Icons/Weapons/Guns]]",
                        "Assets/2DArt/UI/Items/Item_Icons/Weapons/Guns")
    assert ensure_category(text, "Assets/2DArt/UI/Items/Item_Icons/Weapons/Guns") == text
    assert ensure_category("x", "A/B").endswith("[[Category:A/B]]")


def test_mark_historical_once():
    marked = mark_historical("== Licensing ==", "3.0.27.157000")
    assert marked.startswith("{{Historical content|Removed in [[Changelog:V3.0.27.157000]]}}\n")
    assert mark_historical(marked, "V9") == marked


def test_redirect_page_round_trip():
    text = redirect_page("ITEM_Pistol_Handgun.png")
    assert text == "#REDIRECT [[File:ITEM_Pistol_Handgun.png]]\n[[Category:Image Redirects]]"
    assert redirect_target("#REDIRECT [[FILE:ITEM_Pistol_Handgun.png]]") == "ITEM_Pistol_Handgun.png"


def test_redirect_action_never_overrides_editors():
    from assetsync.redirects import DONE, KEEP, WRITE, redirect_action

    item = {"redirect": "ITEM_Hot Coffee.png", "target": "ITEM_Thermos.png"}
    assert redirect_action(False, None, item) == WRITE
    assert redirect_action(True, "ITEM Thermos.png", item) == DONE
    # An editor pointed it at a nicer icon: keep it.
    assert redirect_action(True, "T ITEM DRINK Coffee.png", item) == KEEP
    # A real file (not a redirect) under the display name: keep it.
    assert redirect_action(True, None, item) == KEEP
    # Still where the bot last pointed it, and the game's icon moved: follow the game.
    moved = {**item, "previous": "ITEM_Old_Thermos.png"}
    assert redirect_action(True, "ITEM Old Thermos.png", moved) == WRITE
