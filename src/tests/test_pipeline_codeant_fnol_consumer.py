"""Consumer proof; canonical normalizer fix belongs to Dojo, not this repo."""
import pytest
from langchain_agents.doc_inventories import normalize_sorter_subclass

@pytest.mark.parametrize("token", ["auto", "property", "liability", "health", "life", "workers_comp"])
def test_canonical_producer_fnol_token_retains_identity(token):
    assert normalize_sorter_subclass("insurance_claim", token) == token
