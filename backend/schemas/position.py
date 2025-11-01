from pydantic import BaseModel


class PositionOut(BaseModel):
    id: int
    user_id: int
    symbol: str
    name: str
    market: str
    quantity: int
    available_quantity: int
    avg_cost: float
    leverage: int

    class Config:
        from_attributes = True
