from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from prepared_hotwords import load_prepared_hotword_terms


class PreparedHotwordsTests(unittest.TestCase):
    def test_loads_canonical_terms_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "course" / "lectures" / "lecture" / "prepared" / "hotwords.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "lecture_id": "lecture",
                        "hotwords": [
                            {"term": " Net present value "},
                            {"term": "Project OT"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            terms = load_prepared_hotword_terms(
                root, course_id="course", lecture_id="lecture"
            )

        self.assertEqual(terms, ("Net present value", "Project OT"))

    def test_missing_or_invalid_artifact_disables_hotwords(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                load_prepared_hotword_terms(
                    root, course_id="course", lecture_id="lecture"
                ),
                (),
            )
            path = root / "course" / "lectures" / "lecture" / "prepared" / "hotwords.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "lecture_id": "another-lecture",
                        "hotwords": [{"term": "NPV"}],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                load_prepared_hotword_terms(
                    root, course_id="course", lecture_id="lecture"
                ),
                (),
            )

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                load_prepared_hotword_terms(
                    Path(directory), course_id="../outside", lecture_id="lecture"
                ),
                (),
            )


if __name__ == "__main__":
    unittest.main()
