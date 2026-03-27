from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Quote, QuoteStatus, Submission, SubmissionStatus, User, UserRole


def iso(days_from_today: int) -> str:
    return (date.today() + timedelta(days=days_from_today)).isoformat()


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def reset_and_seed(db_path: str) -> None:
    ensure_parent(db_path)
    if os.path.exists(db_path):
        os.remove(db_path)

    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)

    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        user = User(
            username='renewal_tester',
            full_name='Renewal Test User',
            role=UserRole.ADMIN,
            is_active=True,
        )
        user.set_password('demo123!')
        session.add(user)
        session.flush()

        atlas = Submission(
            insured_name='Atlas Manufacturing',
            effective_date=iso(200),
            state='TX',
            status=SubmissionStatus.RECEIVED,
            assigned_to=user.id,
        )
        beacon = Submission(
            insured_name='Beacon Logistics',
            effective_date=iso(45),
            state='IL',
            status=SubmissionStatus.CHOSEN,
            assigned_to=user.id,
        )
        cascade = Submission(
            insured_name='Cascade Foods',
            effective_date=iso(-12),
            state='CA',
            status=SubmissionStatus.SENT_TO_FINANCE,
            assigned_to=user.id,
        )
        delta = Submission(
            insured_name='Delta Services',
            effective_date=iso(150),
            state='CA',
            status=SubmissionStatus.CHOSEN,
            assigned_to=user.id,
        )
        evergreen = Submission(
            insured_name='Evergreen Transport',
            effective_date=iso(12),
            state='WA',
            status=SubmissionStatus.IN_PROGRESS,
            assigned_to=user.id,
        )
        frontier = Submission(
            insured_name='Frontier Warehousing',
            effective_date=iso(150),
            state='OK',
            status=SubmissionStatus.IN_PROGRESS,
            assigned_to=user.id,
        )

        session.add_all([atlas, beacon, cascade, delta, evergreen, frontier])
        session.flush()

        # Add quotes for Atlas (needed for "Submit to Market" test)
        session.add(Quote(
            submission_id=atlas.id,
            carrier_name='Acme Specialty',
            raw_document_path='/tmp/atlas-quote-1.pdf',
            extracted_json='{}',
            status=QuoteStatus.RECEIVED,
        ))

        # Add quotes for Frontier (needed for "Bind" test)
        session.add(Quote(
            submission_id=frontier.id,
            carrier_name='Acme Specialty',
            raw_document_path='/tmp/frontier-warehousing-quote.pdf',
            extracted_json='{}',
            status=QuoteStatus.RECEIVED,
        ))
        session.commit()
    finally:
        session.close()


if __name__ == '__main__':
    db_path = os.environ.get('DATABASE_PATH')
    if not db_path:
        raise SystemExit('DATABASE_PATH env var is required for e2e seed setup')
    reset_and_seed(db_path)
    print(f'Seeded e2e test database: {db_path}')
