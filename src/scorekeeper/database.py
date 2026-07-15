from datetime import datetime

from sqlalchemy import DateTime, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from scorekeeper.config import get_settings


class Base(DeclarativeBase):
    pass


class Score(Base):
    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    player: Mapped[str] = mapped_column(String(100), index=True)
    points: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.now)

    def as_dict(self) -> dict[str, int | str]:
        return {
            "id": self.id,
            "player": self.player,
            "points": self.points,
            "created_at": self.created_at.isoformat(),
        }


engine = create_engine(get_settings().database_url, pool_pre_ping=True)


def create_schema() -> None:
    Base.metadata.create_all(engine)


def add_score(player: str, points: int) -> dict[str, int | str]:
    with Session(engine) as session:
        score = Score(player=player, points=points)
        session.add(score)
        session.commit()
        session.refresh(score)
        return score.as_dict()


def get_scores(limit: int = 50) -> list[dict[str, int | str]]:
    with Session(engine) as session:
        query = select(Score).order_by(Score.created_at.desc()).limit(limit)
        return [score.as_dict() for score in session.scalars(query)]

