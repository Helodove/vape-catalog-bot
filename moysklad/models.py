from pydantic import BaseModel, Field
from typing import Optional


class ProductFolder(BaseModel):
    id: str
    name: str
    href: str
    path_name: str = Field(default="", alias="pathName")
    parent_href: Optional[str] = None

    model_config = {"populate_by_name": True}


class PriceType(BaseModel):
    name: str


class SalePrice(BaseModel):
    value: float
    price_type: PriceType = Field(alias="priceType")

    model_config = {"populate_by_name": True}


class Attribute(BaseModel):
    name: str
    value: Optional[object] = None


class Product(BaseModel):
    id: str
    name: str
    href: str
    code: Optional[str] = None
    description: Optional[str] = None
    sale_prices: list[SalePrice] = Field(default_factory=list, alias="salePrices")
    attributes: list[Attribute] = Field(default_factory=list)
    stock: Optional[float] = None
    image_url: Optional[str] = None

    model_config = {"populate_by_name": True}

    @property
    def retail_price(self) -> Optional[float]:
        for p in self.sale_prices:
            if p.price_type.name == "Цена продажи":
                return p.value / 100
        if self.sale_prices:
            return self.sale_prices[0].value / 100
        return None

    @property
    def in_stock(self) -> bool:
        return bool(self.stock and self.stock > 0)
