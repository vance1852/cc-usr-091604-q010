import unittest

from app.tickets import Ticket, TicketService


class TicketSmokeTest(unittest.TestCase):
    def test_ticket_and_health(self):
        self.assertEqual(Ticket("match-1", "A-12").seat, "A-12")
        self.assertEqual(TicketService().health()["status"], "ok")


if __name__ == "__main__":
    unittest.main()

