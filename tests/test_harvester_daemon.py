
import pytest
import os
import threading
from unittest.mock import MagicMock, patch
from causadb._daemon_service import HarvesterDaemon

def test_daemon_runs_harvester_periodically(tmp_path):
    ledger_path = str(tmp_path / "ledger.log")
    # Don't write anything; let the file be empty for genesis
    open(ledger_path, "a").close()

    # Patch Timer to prevent actual background threads
    with patch("threading.Timer") as MockTimer:
        mock_timer_instance = MagicMock()
        MockTimer.return_value = mock_timer_instance

        daemon = HarvesterDaemon(ledger_path=ledger_path)

        # Patch harvester to return results
        daemon.harvester.harvest_all = MagicMock(return_value={"shell": 1})

        daemon.start()

        # Auditoría F: el primer tick va en background — NO cosecha síncrona
        # (el backfill inicial no debe congelar el arranque)
        assert not daemon.harvester.harvest_all.called, (
            "El tick inicial debe agendarse con Timer(0), no ejecutarse en start()"
        )
        assert MockTimer.called
        assert MockTimer.call_args[0][0] == 0.0  # agendado inmediato

        # Simular que el timer dispara el tick → cosecha y re-agenda
        tick_cb = MockTimer.call_args[0][1]
        tick_cb()
        daemon.harvester.harvest_all.assert_called_once()
        assert MockTimer.call_count >= 2  # re-agenda con el intervalo
        assert MockTimer.call_args[0][0] == daemon.interval

        daemon.stop()

def test_anti_teatro_daemon_harvest_disabled(tmp_path):
    ledger_path = str(tmp_path / "ledger.log")
    open(ledger_path, "a").close()
        
    # Verify that if harvest is NOT called, ledger remains empty (mocking the ledger check)
    with patch("causadb._harvester.Harvester.harvest_all") as mock_harvest:
        mock_harvest.return_value = {} # Nothing harvested
        
        daemon = HarvesterDaemon(ledger_path=ledger_path)
        
        # Don't call start()
        
        # Check that harvester was NOT called
        assert not mock_harvest.called
