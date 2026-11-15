# Put back hand-picked hikes that were dropped only because the Overpass downloads lacked their lake / trailhead.
# The server's fill.sh reroutes any listed hike that isn't routed yet; a second failure just leaves it unrouted.
import json, os, sys
H = sys.argv[1]   # the hikes/ directory
sys.path.insert(0, os.path.dirname(H.rstrip("/")))
import adopt

R = {
 "or-mt-jefferson-santiam-pass": [
  {"id": "canyon-creek-meadows", "name": "Canyon Creek Meadows", "type": "out", "rank": 3, "start": ["Jack Lake Trailhead", 44.49184, -121.79417], "to": ["Canyon Creek Meadows", 44.4924909, -121.830254], "mi": 7.5, "blurb": "Wildflower meadows climbing toward Three Fingered Jack's jagged east face and a glacial cirque."},
  {"id": "marion-lake", "name": "Marion Lake and Marion Falls", "type": "out", "rank": 8, "start": ["Marion Lake Trailhead", 44.57691, -121.89406], "to": ["Marion Lake", 44.5569384, -121.859819], "mi": 5.0, "blurb": "A gentle climb to a big lake with Three Fingered Jack beyond and a two-tier falls just below its outlet."},
  {"id": "triangulation-peak", "name": "Triangulation Peak and Boca Cave", "type": "out", "rank": 11, "start": ["Triangulation Trailhead", 44.677, -121.815], "to": ["Triangulation Peak", 44.6875, -121.832], "mi": 4.2, "blurb": "A short ridge walk to a rocky summit and a hidden cave framing Mt Jefferson."},
  {"id": "square-lake", "name": "Square Lake", "type": "out", "rank": 18, "start": ["Square Lake Trailhead", 44.425, -121.81], "to": ["Square Lake", 44.4357, -121.82017], "mi": 4.4, "blurb": "A short walk off Santiam Pass to a lake with Three Fingered Jack's south face above."}],
 "wa-north-cascades-highway-methow": [
  {"id": "thornton-lakes", "name": "Thornton Lakes", "type": "out", "rank": 9, "start": ["Thornton Lakes Trailhead", 48.656, -121.325], "to": ["Lower Thornton Lake", 48.68, -121.33], "mi": 10.0, "blurb": "Up an old logging road and steep trail to a granite-rimmed lake below Trappers Peak."},
  {"id": "thunder-knob", "name": "Thunder Knob", "type": "out", "rank": 10, "start": ["Colonial Creek Campground", 48.691, -121.096], "to": ["Thunder Knob", 48.698, -121.084], "mi": 3.6, "blurb": "An easy climb to rocky viewpoints over the startling green of Diablo Lake."},
  {"id": "sourdough-mountain", "name": "Sourdough Mountain Lookout", "type": "out", "rank": 6, "start": ["Sourdough Mountain Trailhead", 48.714, -121.14], "to": ["Sourdough Mountain Lookout", 48.736, -121.126], "mi": 10.4, "blurb": "One of the steepest trails around, 5,000 feet straight up to a lookout over Diablo and Ross Lakes."},
  {"id": "twisp-pass", "name": "Twisp Pass", "type": "out", "rank": 17, "start": ["Twisp River Trailhead", 48.452, -120.617], "to": ["Twisp Pass", 48.46736, -120.64351], "mi": 8.4, "blurb": "Up the head of the Twisp River to a pass looking into the North Cascades park."},
  {"id": "grasshopper-pass", "name": "Grasshopper Pass", "type": "out", "rank": 8, "start": ["Harts Pass Trailhead", 48.72129, -120.67006], "to": ["Grasshopper Pass", 48.678, -120.662], "mi": 11.0, "blurb": "South on the Pacific Crest Trail from the state's highest road along open, larch-spotted ridges."}],
 "wa-leavenworth-enchantments-wenatchee": [
  {"id": "lake-ingalls-lake", "name": "Lake Ingalls", "type": "out", "rank": 2, "start": ["Esmeralda Trailhead", 47.436, -120.936], "via": [["Ingalls Pass", 47.45933, -120.93537]], "to": ["Lake Ingalls", 47.471214, -120.9381403], "mi": 9.0, "blurb": "Over a larch-filled pass to a granite lake at the foot of Mt Stuart, mountain goats likely."},
  {"id": "lake-caroline", "name": "Lake Caroline", "type": "out", "rank": 8, "start": ["Eightmile Trailhead", 47.553, -120.776], "to": ["Lake Caroline", 47.539, -120.835], "mi": 11.0, "blurb": "Past Eightmile Lake and up switchbacks to a high lake with Cashmere Mountain and the Stuart Range around it."}],
 "wa-stevens-pass-us-2": [
  {"id": "lake-serene", "name": "Lake Serene and Bridal Veil Falls", "type": "out", "rank": 1, "start": ["Lake Serene Trailhead", 47.809, -121.574], "to": ["Lake Serene", 47.788, -121.568], "mi": 8.2, "blurb": "Past a tall, many-stranded falls and up hundreds of steps to a lake sitting under Mt Index's sheer north wall."},
  {"id": "lake-valhalla", "name": "Lake Valhalla", "type": "out", "rank": 3, "start": ["Smithbrook Trailhead", 47.814, -121.08], "to": ["Lake Valhalla", 47.7893561, -121.1004988], "mi": 7.0, "blurb": "Over Union Gap on the Pacific Crest Trail to a sandy-beached lake under the cliffs of Lichtenberg Mountain."},
  {"id": "barclay-lake", "name": "Barclay Lake", "type": "out", "rank": 8, "start": ["Barclay Lake Trail Head", 47.79239, -121.4593], "to": ["Barclay Lake", 47.788, -121.443], "mi": 4.4, "blurb": "A nearly flat forest walk to a lake right under Baring Mountain's 3,000-foot north face."},
  {"id": "lake-janus-grizzly", "name": "Lake Janus", "type": "out", "rank": 10, "start": ["Smithbrook Trailhead", 47.814, -121.08], "to": ["Lake Janus", 47.837, -121.146], "mi": 7.0, "blurb": "North on the Pacific Crest Trail through meadows to a quiet lake below Jove Peak."}],
 "wa-bellingham-mt-baker": [
  {"id": "oyster-dome", "name": "Oyster Dome", "type": "out", "rank": 12, "start": ["Oyster Dome Trailhead", 48.605, -122.438], "to": ["Oyster Dome", 48.62693, -122.41482], "mi": 5.0, "blurb": "Up the Chuckanuts to a rock outcrop high above Samish Bay and the San Juans."}],
 "wa-snoqualmie-pass": [
  {"id": "gem-lake", "name": "Gem Lake", "type": "out", "rank": 6, "start": ["Snow Lake Trailhead", 47.445, -121.423], "to": ["Gem Lake", 47.471, -121.457], "mi": 10.0, "blurb": "Past Snow Lake and up to a smaller, rockier lake with views across to Chair Peak."}],
 "or-bend-smith-rock-newberry": [
  {"id": "smith-rock-misery-ridge", "name": "Smith Rock: Misery Ridge and Monkey Face", "type": "loop", "rank": 1, "start": ["Smith Rock State Park", 44.3666, -121.1377], "via": [["Monkey Face", 44.3725, -121.1475]], "mi": 3.8, "blurb": "Steep stairs over a ridge of tuff cliffs, down past the overhanging Monkey Face spire and back along the Crooked River."},
  {"id": "benham-falls", "name": "Benham Falls", "type": "out", "rank": 7, "start": ["Benham Falls Day Use Area", 43.927, -121.411], "to": ["Benham Falls", 43.93899, -121.4129], "mi": 1.5, "blurb": "Over a footbridge and along the Deschutes to where it squeezes through a lava flow in a long, loud cascade."}],
 "wa-olympic-coast-hoh-quinault": [
  {"id": "second-beach", "name": "Second Beach", "type": "out", "rank": 3, "start": ["Second Beach Trailhead", 47.898, -124.624], "to": ["Second Beach", 47.889, -124.625], "mi": 1.4, "blurb": "A short forest walk to a wide beach with sea stacks lined up offshore, a classic sunset spot."},
  {"id": "shi-shi-beach", "name": "Shi Shi Beach and Point of the Arches", "type": "out", "rank": 5, "start": ["Shi Shi Beach Trailhead", 48.293, -124.665], "to": ["Point of the Arches", 48.249, -124.699], "mi": 8.0, "blurb": "A muddy forest trail, then two miles of wild beach to a row of sea stacks and arches."},
  {"id": "cape-flattery", "name": "Cape Flattery", "type": "out", "rank": 6, "start": ["Cape Flattery Trailhead", 48.384, -124.715], "to": ["Cape Flattery", 48.38552, -124.7258], "mi": 1.5, "blurb": "Boardwalk through forest to cliff-edge platforms at the northwest tip of the lower 48."},
  {"id": "mount-storm-king", "name": "Mount Storm King", "type": "out", "rank": 10, "start": ["Storm King Ranger Station", 48.058, -123.788], "to": ["Mount Storm King", 48.05537, -123.74075], "mi": 4.2, "blurb": "Steep switchbacks to a ridge over Lake Crescent's deep blue water (the last bit is roped scrambling)."},
  {"id": "marymere-falls", "name": "Marymere Falls", "type": "out", "rank": 11, "start": ["Storm King Ranger Station", 48.058, -123.788], "to": ["Marymere Falls", 48.048, -123.786], "mi": 1.8, "blurb": "An easy walk under big trees to a 90-foot falls in a fern grotto."}],
 "or-central-coast": [
  {"id": "heceta-head-lighthouse", "name": "Heceta Head Lighthouse", "type": "out", "rank": 2, "start": ["Heceta Head Lighthouse State Scenic Viewpoint", 44.136, -124.124], "to": ["Heceta Head Lighthouse", 44.1374, -124.128], "mi": 1.0, "blurb": "From the beach cove up past the keeper's house to the coast's brightest lighthouse."},
  {"id": "kentucky-falls", "name": "Kentucky Falls", "type": "out", "rank": 3, "start": ["Kentucky Falls Trailhead", 44.004, -123.79], "to": ["Lower Kentucky Falls", 43.998, -123.778], "mi": 4.4, "blurb": "Through old Coast Range forest past one falls to a viewing deck between two more."}],
 "or-curry-coast-kalmiopsis": [
  {"id": "cape-sebastian", "name": "Cape Sebastian", "type": "out", "rank": 4, "start": ["Cape Sebastian State Scenic Corridor", 42.327, -124.425], "to": ["Cape Sebastian", 42.32, -124.427], "mi": 3.0, "blurb": "Out a high headland through spruce to the cape's tip, the coast running south toward California."}],
 "or-crater-lake-mt-thielsen": [
  {"id": "lemolo-falls", "name": "Lemolo Falls", "type": "out", "rank": 10, "start": ["Lemolo Falls Trailhead", 43.339, -122.22], "to": ["Lemolo Falls", 43.3458, -122.2202], "mi": 3.4, "blurb": "Down the North Umpqua to a 100-foot falls thundering into a deep pool."}],
 "or-umpqua-roseburg": [
  {"id": "umpqua-hot-springs", "name": "Umpqua Hot Springs", "type": "out", "rank": 2, "start": ["Umpqua Hot Springs Trailhead", 43.293, -122.365], "to": ["Umpqua Hot Springs", 43.29677, -122.36377], "mi": 0.6, "blurb": "A short climb to hot pools on a travertine ledge above the North Umpqua."},
  {"id": "fall-creek-falls-umpqua", "name": "Fall Creek Falls", "type": "out", "rank": 4, "start": ["Fall Creek Falls Trailhead", 43.313, -122.834], "to": ["Fall Creek Falls", 43.31907, -122.83985], "mi": 1.8, "blurb": "Through a rock crevice and past Jobs Garden to a two-tier falls."}],
 "or-clackamas-opal-creek-bull-of-the-woods": [
  {"id": "pansy-lake", "name": "Pansy Lake", "type": "out", "rank": 6, "start": ["Pansy Basin Trailhead", 44.902, -122.135], "to": ["Pansy Lake", 44.8856, -122.1142], "mi": 2.6, "blurb": "A short walk through big trees and a meadow to a lake under Pansy Mountain."},
  {"id": "olallie-butte", "name": "Olallie Butte", "type": "out", "rank": 5, "start": ["Olallie Butte Trailhead", 44.823, -121.749], "to": ["Olallie Butte", 44.82067, -121.76368], "mi": 7.4, "blurb": "A steady climb up a lone cone above the Olallie Lakes, with Jefferson's north face right next door."}],
 "wa-san-juans-whidbey": [
  {"id": "goose-rock", "name": "Goose Rock at Deception Pass", "type": "out", "rank": 3, "start": ["Deception Pass State Park", 48.406, -122.645], "to": ["Goose Rock", 48.406, -122.639], "mi": 2.5, "blurb": "Under the Deception Pass bridge and up to a rocky bald over the churning channel."}],
 "wa-north-bend-mt-si": [
  {"id": "snoqualmie-falls", "name": "Snoqualmie Falls", "type": "out", "rank": 9, "start": ["Snoqualmie Falls Park", 47.5414, -121.8374], "to": ["Snoqualmie Falls", 47.5419, -121.8379], "mi": 1.4, "blurb": "Down to the base of a 268-foot falls, Washington's most famous, from the overlook above."}],
 "or-portland-vancouver": [
  {"id": "warrior-rock", "name": "Warrior Rock Lighthouse", "type": "out", "rank": 3, "start": ["Warrior Point Trailhead", 45.829, -122.784], "to": ["Warrior Rock Lighthouse", 45.849, -122.788], "mi": 7.0, "blurb": "Along Sauvie Island's Columbia beaches under cottonwoods to a small lighthouse at the island's tip."}],
}
n = 0
for sid, hikes in R.items():
    f = os.path.join(H, sid + ".json")
    d = json.load(open(f, encoding="utf-8"))
    have = {h["id"] for h in d["hikes"]}
    add = [h for h in hikes if h["id"] not in have]
    d["hikes"].extend(add)
    open(f, "w", encoding="utf-8").write(adopt.dumps(d))
    n += len(add)
print("restored", n)
