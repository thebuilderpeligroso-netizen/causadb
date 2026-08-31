import pytest
import json
from causadb.cli._cmd_snapshot import cmd_snapshot
from unittest.mock import MagicMock, patch

def test_snapshot_with_chronicle_ref(tmp_path):
    ledger_path = tmp_path / "ledger.log"
    ledger_path.write_text("{}")
    
    args = MagicMock()
    args.ledger = str(ledger_path)
    args.tests = 10
    args.fases = "1,2"
    args.bloqueantes = 0
    args.notas = "test"
    args.chronicle_ref = "BIT-TEST"

    with patch("causadb._workspace.resolve_ledger", return_value=str(ledger_path)), \
         patch("causadb.cli._cmd_snapshot.LedgerWriter") as mock_writer, \
         patch("causadb.cli._cmd_snapshot.LedgerReader") as mock_reader, \
         patch("causadb._chronicle_index.link_events") as mock_link:
        
        # Mock LedgerReader's read_all_entries method
        mock_reader.return_value.read_all_entries.return_value = []

        # Mock LedgerWriter's append method
        mock_writer_instance = mock_writer.return_value
        mock_writer_instance.append.return_value = {
            "event": {"event_id": "uuid-123"},
            "hash": "hash-abc"
        }
        
        code, output = cmd_snapshot(args)
        
        assert code == 0
        mock_link.assert_called_once_with(str(ledger_path), "BIT-TEST", ["uuid-123"])
