"""MongoDB models for Stock and MarketData collections."""
from mongoengine import Document, fields
from datetime import datetime, timezone


class Stock(Document):
    """Stock information stored in MongoDB Atlas."""
    _id = fields.StringField(primary_key=True)  # Symbol
    stock_id = fields.IntField(required=True, db_field='stockId')
    company = fields.StringField()
    sector = fields.StringField()
    sub_sector = fields.StringField(db_field='subSector')
    country = fields.StringField()
    price = fields.FloatField()
    quantity = fields.LongField()
    last_updated = fields.DateTimeField(db_field='lastUpdated')
    
    meta = {'collection': 'stocks'}
    
    @property
    def symbol(self):
        return self._id


class MarketData(Document):
    """Historical market data stored in MongoDB Atlas."""
    date_time = fields.DateTimeField(required=True, db_field='dateTime')
    instrument = fields.StringField(required=True)
    open = fields.FloatField()
    high = fields.FloatField()
    low = fields.FloatField()
    close = fields.FloatField()
    volume = fields.LongField()
    vwap = fields.FloatField()
    amount = fields.FloatField()
    factor = fields.FloatField(default=1.0)
    turnover = fields.FloatField()
    float_shares = fields.LongField(db_field='floatShares')
    created_at = fields.DateTimeField(db_field='createdAt')
    
    meta = {'collection': 'marketData'}
    
    @property
    def datetime(self):
        return self.date_time