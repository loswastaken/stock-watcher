"""Supported retailers: host matching, display metadata and which adapter handles each one.

An adapter is ``"module:function"`` under ``app.checkers.retailers``; ``None`` means the
platform recipes + generic checker handle the site on their own.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Retailer:
    key: str
    name: str
    hosts: tuple[str, ...]
    adapter: str | None = None
    color: str = "#64748b"
    pickup: bool = False  # in-store pickup by ZIP
    seller_filter: bool = False  # can tell first-party from marketplace sellers
    browser: bool = False  # known to need a real browser
    note: str | None = None

    @property
    def domain(self) -> str:
        return self.hosts[0]

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "domain": self.domain,
            "hosts": list(self.hosts),
            "color": self.color,
            "pickup": self.pickup,
            "seller_filter": self.seller_filter,
            "note": self.note,
        }


R = Retailer

RETAILERS: list[Retailer] = [
    R("amd", "AMD", ("amd.com",), "electronics:amd", "#000000", browser=True),
    R("asus", "ASUS", ("shop.asus.com", "store.asus.com", "asus.com"), None, "#00539b"),
    R("acegraphics", "Ace Graphics Cards", ("acegraphicscards.com",), None, "#b91c1c"),
    R("adorama", "Adorama", ("adorama.com",), "electronics:adorama", "#0a4595", browser=True),
    R("amazon", "Amazon", ("amazon.com", "smile.amazon.com"), "amazon:check", "#ff9900", seller_filter=True),
    R("antonline", "Antonline", ("antonline.com",), "electronics:antonline", "#c8102e"),
    R("bhphoto", "B&H Photo Video", ("bhphotovideo.com",), "electronics:bhphoto", "#d7282f", browser=True),
    R("bjs", "BJs", ("bjs.com",), "bigbox:bjs", "#d71920", browser=True),
    R("bandai", "Bandai Namco", ("p-bandai.com", "bandainamcoent.com"), "games:bandai", "#1f1f1f"),
    R("bestbuy", "Best Buy", ("bestbuy.com",), "bestbuy:check", "#0046be", pickup=True, seller_filter=True),
    R("canon", "Canon", ("usa.canon.com", "shop.usa.canon.com"), None, "#cc0000"),
    R("consutronix", "Consutronix", ("consutronix.com",), None, "#dc2626"),
    R("costco", "Costco", ("costco.com",), "bigbox:costco", "#e31837", browser=True),
    R("dell", "Dell", ("dell.com",), "electronics:dell", "#007db8", browser=True),
    R("disney", "Disney", ("disneystore.com", "shopdisney.com"), None, "#113ccf"),
    R("evga", "EVGA", ("evga.com",), "electronics:evga", "#1f1f1f"),
    R("fujifilm", "Fujifilm", ("shopusa.fujifilm-x.com", "fujifilm-x.com", "fujifilm.com"), None, "#1f1f1f"),
    R("gamefly", "GameFly", ("gamefly.com",), None, "#1d4ed8"),
    R("gamestop", "GameStop", ("gamestop.com",), "games:gamestop", "#000000", browser=True),
    R("gigabyte", "Gigabyte", ("gigabyte.com", "aorus.com"), "electronics:gigabyte", "#1f1f1f"),
    R("govee", "Govee", ("govee.com",), None, "#3b82f6"),
    R("hallmark", "Hallmark", ("hallmark.com",), None, "#5b2c83"),
    R("homedepot", "Home Depot", ("homedepot.com",), "bigbox:homedepot", "#f96302", browser=True),
    R("jazwares", "Jazwares", ("jazwares.com", "squishmallows.com"), None, "#1d4ed8"),
    R("kohls", "Kohls", ("kohls.com",), "bigbox:kohls", "#1f1f1f", browser=True),
    R("kroger", "Kroger",
      ("kroger.com", "ralphs.com", "fredmeyer.com", "kingsoopers.com", "frysfood.com", "smithsfoodanddrug.com",
       "qfc.com", "dillons.com", "marianos.com", "picknsave.com", "harristeeter.com", "foodsco.net", "citymarket.com"),
      "kroger:check", "#134b97", pickup=True, browser=True),
    R("lg", "LG", ("lg.com",), "electronics:lg", "#a50034", browser=True),
    R("lego", "Lego", ("lego.com",), "games:lego", "#e3000b"),
    R("leica", "Leica", ("leica-camera.com",), "electronics:leica", "#e20612"),
    R("lenovo", "Lenovo", ("lenovo.com",), "electronics:lenovo", "#e2231a", browser=True),
    R("msi", "MSI", ("us-store.msi.com", "msi.com"), None, "#1f1f1f"),
    R("mattel", "Mattel", ("shop.mattel.com", "creations.mattel.com", "mattel.com"), None, "#e4002b"),
    R("meijer", "Meijer", ("meijer.com",), "bigbox:meijer", "#d50032", browser=True),
    R("microcenter", "Micro Center", ("microcenter.com",), "electronics:microcenter", "#1f1f1f", pickup=True),
    R("microsoft", "Microsoft", ("microsoft.com",), "microsoft:check", "#737373"),
    R("xbox", "Microsoft Xbox", ("xbox.com",), "microsoft:check", "#107c10"),
    R("nyxi", "NYXI", ("nyxigame.com",), None, "#2563eb"),
    R("neutronusa", "NeutronUSA", ("neutronusa.com",), None, "#1e3a8a"),
    R("newegg", "Newegg", ("newegg.com",), "electronics:newegg", "#f7a800", seller_filter=True),
    R("nextwarehouse", "NextWarehouse", ("nextwarehouse.com",), None, "#be123c"),
    R("ninja", "Ninja Kitchen", ("ninjakitchen.com",), None, "#1f1f1f"),
    R("nintendo", "Nintendo", ("nintendo.com",), "games:nintendo", "#e60012"),
    R("nvidia", "Nvidia", ("nvidia.com", "marketplace.nvidia.com", "store.nvidia.com"), "electronics:nvidia",
      "#76b900"),
    R("oculus", "Oculus", ("meta.com", "oculus.com"), "electronics:meta_quest", "#1f1f1f", browser=True),
    R("officedepot", "Office Depot", ("officedepot.com", "officemax.com"), "bigbox:officedepot", "#cc0000",
      browser=True),
    R("popmart", "POP MART", ("popmart.com",), "games:popmart", "#e60012", browser=True),
    R("playasia", "Play-Asia", ("play-asia.com",), "games:playasia", "#6aa84f"),
    R("psdirect", "PlayStation Direct", ("direct.playstation.com",), "games:psdirect", "#003791"),
    R("pokemoncenter", "Pokemon Center", ("pokemoncenter.com",), "games:pokemoncenter", "#e3350d", browser=True),
    R("qvc", "QVC.com", ("qvc.com",), "bigbox:qvc", "#e4002b", browser=True),
    R("robertscamera", "Roberts Camera", ("robertscamera.com",), None, "#1f1f1f"),
    R("samsclub", "Sam's club", ("samsclub.com",), "walmart:check", "#0067a0", seller_filter=True, browser=True),
    R("stockx", "StockX", ("stockx.com",), "bigbox:stockx", "#006340", browser=True,
      note="Resale marketplace: 'in stock' means someone is selling it"),
    R("target", "Target", ("target.com",), "target:check", "#cc0000", pickup=True, seller_filter=True,
      note="Delivery or in-store pickup near your ZIP"),
    R("toysrus", "ToysRUs", ("toysrus.com", "macys.com"), "bigbox:toysrus", "#1c4aa3", browser=True,
      note="US Toys\"R\"Us online store runs on macys.com"),
    R("verizon", "Verizon", ("verizon.com",), "bigbox:verizon", "#cd040b", browser=True),
    R("walmart", "Walmart", ("walmart.com",), "walmart:check", "#0071ce", seller_filter=True),
    R("zotac", "Zotac", ("zotacstore.com", "zotac.com"), "electronics:zotac", "#1f1f1f"),
    R("ebay", "eBay", ("ebay.com",), "bigbox:ebay", "#e53238",
      note="Individual listings: 'in stock' means the listing is live with quantity left"),
]

# Longest host first so "creations.mattel.com" wins over "mattel.com".
_HOST_INDEX: list[tuple[str, Retailer]] = sorted(
    ((h, r) for r in RETAILERS for h in r.hosts), key=lambda p: len(p[0]), reverse=True
)
_BY_KEY = {r.key: r for r in RETAILERS}


def _host(url: str) -> str:
    try:
        host = (urlsplit(url if "//" in url else f"https://{url}").hostname or "").lower()
    except ValueError:
        return ""
    return host.removeprefix("www.")


def match_retailer(url: str) -> Retailer | None:
    host = _host(url or "")
    if not host:
        return None
    for h, r in _HOST_INDEX:
        if host == h or host.endswith("." + h):
            return r
    return None


def retailer_by_key(key: str) -> Retailer | None:
    return _BY_KEY.get(key)
