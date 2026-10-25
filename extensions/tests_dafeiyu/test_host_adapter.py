import tempfile
import unittest
from pathlib import Path

from maibot_unified_core.host_adapter import _hydrate_search_paragraphs
from maibot_unified_core.store import State


class HostProvenanceTests(unittest.TestCase):
    def store(self, rows):
        class RunningStore:
            def get_paragraph(self, key):
                return rows.get(key)
        return RunningStore()

    def test_authoritative_metadata_replaces_retrieval_permissions(self):
        payload = {"hits": [{"hash": "p", "type": "paragraph", "source": "sparse_bm25",
                             "content": "stale", "score": 0.7,
                             "metadata": {"public_summary": True, "chat_id": "wrong", "category": "AI"}}],
                   "summary": "unfiltered summary"}
        result = _hydrate_search_paragraphs(payload, self.store({"p": {
            "hash": "p", "source": "chat_summary:A", "content": "authoritative",
            "metadata": {"chat_id": "A", "source_type": "person_fact", "public_summary": False}}}))
        hit = result["hits"][0]
        self.assertEqual(hit["content"], "authoritative")
        self.assertEqual(hit["source"], "chat_summary:A")
        self.assertEqual(hit["score"], 0.7)
        self.assertEqual(hit["metadata"], {"chat_id": "A", "source_type": "person_fact", "public_summary": False})
        self.assertEqual(result["summary"], "")
        self.assertTrue(result["requires_scope_filter"])

    def test_deleted_unknown_malformed_rows_are_not_returned(self):
        payload = {"hits": [{"hash": key, "type": "paragraph"} for key in ("missing", "deleted", "bad")]}
        rows = {"deleted": {"is_deleted": 1, "metadata": {}}, "bad": {"metadata": "not JSON"}}
        self.assertEqual(_hydrate_search_paragraphs(payload, self.store(rows))["hits"], [])
        self.assertEqual(_hydrate_search_paragraphs(payload, None)["hits"], [])

    def test_scope_and_public_summary_survive_native_sparse_hit(self):
        rows = {"p": {"source": "chat_summary:A", "content": "AI topic summary",
                      "metadata": '{"chat_id":"A","source_type":"chat_summary","public_summary":true,"category":"AI"}'}}
        hit = _hydrate_search_paragraphs({"hits": [{"hash": "p", "type": "paragraph", "metadata": {"word_count": 4}}]}, self.store(rows))["hits"][0]
        source = {"platform": "qq", "conversation_id": hit["metadata"]["chat_id"],
                  "sharing_scope": "policy", "metadata": hit["metadata"], "summary_content": hit["content"]}
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp) / "state.db", {"allowed_qq_sessions": ["A", "B"], "shared_groups": [["A", "B"]], "public_qq_sessions": ["A"], "public_topics": ["AI"]})
            try:
                self.assertTrue(state.allowed(source, "qq", "B", "internal"))
                self.assertTrue(state.allowed(source, "bilibili", "feed", "public"))
                source["metadata"]["public_summary"] = False
                self.assertFalse(state.allowed(source, "bilibili", "feed", "public"))
                self.assertFalse(state.allowed(source, "qq", "unrelated", "internal"))
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
