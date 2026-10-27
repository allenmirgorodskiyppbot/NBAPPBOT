"""Extension points for explicitly authorized market providers. No private APIs or scraping."""
from typing import Protocol
from pathlib import Path

from .core import digest, dumps
from .maintenance import fetch_json
from .offers import import_board


class OfferProvider(Protocol):
    def capture(self) -> dict:
        """Return a complete board snapshot using the documented manual JSON contract."""
        ...


class AuthorizedJSONOffers:
    def __init__(self,url,config,token=None):
        self.url=url; self.config=config; self.token=token

    def capture(self):
        return fetch_json(self.url,self.config,self.token)


def import_provider_board(store,provider:OfferProvider):
    board=provider.capture()
    root=store.root/"board_captures"; root.mkdir(exist_ok=True)
    path=root/(digest(board)+".json")
    path.write_text(dumps(board))
    # The same immutable identifiers, evidence, timestamps and review requirements apply.
    return import_board(store,path)
