"""Offline tests: python test_tracker.py"""
import tracker as t

# Text taken from the real Liverpool v Sunderland match page (2 Dec 2026).
BODY_ONE_LINE = """
19:30/WED 2 DEC
Liverpool
Sunderland
Anfield
Premier League
USEFUL INFORMATION
BUY NOW
Hospitality
Enquire now by calling our Hospitality Sales Team on +44 151 264 2222 (Monday to Friday 10am to 4pm)
REGISTRATION ENDEDTue 17 Nov 2026, 11:00am
Additional Members Sale Registration
Read more
Registration will open online from 11am Tuesday 17 November until 10am Wednesday 18 November.
ON SALE SOONMon 23 Nov 2026, 11:00am
Additional Members Sale
Read more
From 11am Monday 23 November.
REGISTRATION ENDEDTue 17 Nov 2026, 10:00am
Young Adult Area Ballot
Read more
Registration: Opens 10am Tuesday 17 November until 9am Wednesday 18 November.
REGISTRATION ENDEDTue 17 Nov 2026, 10:00am
Local Members Ballot
Read more
REGISTRATION ENDEDTue 17 Nov 2026, 10:00am
Local General Ballot
Read more
Official Club Partners
"""
# Same page if the browser puts the status badge and the date on separate lines.
BODY_SPLIT = BODY_ONE_LINE.replace("ENDEDTue", "ENDED\nTue").replace("SOONMon", "SOON\nMon")

EXPECTED = {
    "Additional Members Sale Registration": "Tue 17 Nov 2026, 11:00am",
    "Additional Members Sale": "Mon 23 Nov 2026, 11:00am",
    "Young Adult Area Ballot": "Tue 17 Nov 2026, 10:00am",
    "Local Members Ballot": "Tue 17 Nov 2026, 10:00am",
    "Local General Ballot": "Tue 17 Nov 2026, 10:00am",
}


def test_parse_both_layouts():
    for body in (BODY_ONE_LINE, BODY_SPLIT):
        got = t.parse_entries(body)
        assert {k: v["date"] for k, v in got.items()} == EXPECTED, got
    assert t.parse_entries(BODY_ONE_LINE)["Additional Members Sale"]["status"] == "ON SALE SOON"
    assert t.parse_entries(BODY_SPLIT)["Additional Members Sale"]["status"] == "ON SALE SOON"


def test_no_info_released_yet():
    page = "19:30/WED 2 DEC\nLiverpool\nSunderland\nAnfield\nUSEFUL INFORMATION\nBUY NOW\nOfficial Club Partners"
    assert t.parse_entries(page) == {}


def test_duplicate_titles():
    body = "ON SALE SOONMon 1 Feb 2027, 9:00am\nMembers Sale\nRead more\nON SALE SOONTue 2 Feb 2027, 9:00am\nMembers Sale\n"
    assert list(t.parse_entries(body)) == ["Members Sale", "Members Sale #2"]


def test_diff_new_and_changed():
    new = t.parse_entries(BODY_ONE_LINE)
    assert len(t.diff_entries({}, new)) == 5  # everything new
    old = {k: v["date"] for k, v in new.items()}
    assert t.diff_entries(old, new) == []  # nothing changed, even if status text flips
    old["Additional Members Sale"] = "Mon 23 Nov 2026, 10:00am"
    del old["Local General Ballot"]
    kinds = sorted((c[0], c[1]) for c in t.diff_entries(old, new))
    assert kinds == [("changed", "Additional Members Sale"), ("new", "Local General Ballot")]


def test_home_filter_on_real_cards():
    base = "https://www.liverpoolfc.com/tickets/match/"
    cases = [
        # (slug, card text, expected)
        ("liverpool-v-manchester-united-english-premier-league-20261122",
         "HOME/AWAY Liverpool Crest 1-manchesterunited-crest 16:30/Sun 22 November LiverpoolManchester United pl-main-logo Premier League Anfield", True),
        ("liverpool-champions-league-fc-porto-202627",
         "HOME/AWAY Liverpool Crest Club Crests - Porto 20:00/Wed 9 December LiverpoolFC Porto Champions League Anfield", True),
        ("liverpool-v-bournemouth-english-league-cup-20261216",
         "20:00/Wed 16 December LiverpoolBournemouth Carabao Cup Anfield", True),
        ("everton-v-liverpool-english-premier-league-20261129",
         "12:00/Sun 29 November EvertonLiverpool Premier League Hill Dickinson Stadium On sale soon", False),
        ("liverpool-champions-league-club-brugge-202627",
         "20:00/Wed 25 November Club BruggeLiverpool Champions League Jan Breydel Stadium Sold out", False),
        ("liverpool-women-v-chelsea-women-english-womens-super-league-20261121",
         "12:45/Sat 21 November LiverpoolChelsea Barclays Women's Super League St Helens Stadium", False),
        ("liverpool-women-v-anything-at-anfield",
         "Liverpool Women v Someone Barclays Women's Super League Anfield", False),
    ]
    for slug, text, want in cases:
        assert t.is_mens_home(base + slug, text) is want, slug


def test_alert_text():
    changes = [("new", "Local General Ballot", "Tue 17 Nov 2026, 10:00am", None),
               ("changed", "Additional Members Sale", "Mon 23 Nov 2026, 11:00am", "Mon 23 Nov 2026, 10:00am")]
    title, msg = t.build_alert("Liverpool v Sunderland", "19:30/WED 2 DEC", changes)
    assert "Liverpool v Sunderland" in title and "Wed 2 Dec" in title
    assert "• Local General Ballot: Tue 17 Nov 2026, 10:00am" in msg and "(CHANGED)" in msg


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print(f"\n{len(fns)} tests passed")
