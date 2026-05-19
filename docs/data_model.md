# Detailed data model

This page shows the full entity relationship diagram for Share Dinkum, including key fields on each entity. For a high-level overview see the [README](../README.md#simplified-overview).

```mermaid
erDiagram
    Account ||--o{ Market : "contains"
    Account ||--o{ ExchangeRate : "tracks"
    Account }o--|| FiscalYearType : "uses"
    FiscalYearType ||--o{ FiscalYear : "defines"

    Market ||--o{ Instrument : "lists"
    Instrument ||--o{ InstrumentPriceHistory : "has"

    Instrument ||--o{ Buy : "purchased via"
    Instrument ||--o{ Sell : "sold via"
    Instrument ||--o{ Dividend : "pays"
    Instrument ||--o{ Distribution : "pays"
    Instrument ||--o{ CostBaseAdjustment : "adjusted by"
    Instrument ||--o{ ShareSplit : "split by"

    Buy ||--o{ Parcel : "creates"
    Parcel ||--o{ Parcel : "bifurcates into"
    Parcel ||--o{ SellAllocation : "consumed by"
    Sell ||--o{ SellAllocation : "allocates to"

    CostBaseAdjustment ||--o{ CostBaseAdjustmentAllocation : "allocates to"
    Parcel ||--o{ CostBaseAdjustmentAllocation : "receives"
    ShareSplit }o--o{ Parcel : "transforms"

    Buy }o--o| ExchangeRate : "uses"
    Sell }o--o| ExchangeRate : "uses"
    Dividend }o--o| ExchangeRate : "uses"
    Distribution }o--o| ExchangeRate : "uses"
    CostBaseAdjustment }o--o| ExchangeRate : "uses"

    Buy }o--|| FiscalYear : "classified into"
    Sell }o--|| FiscalYear : "classified into"
    SellAllocation }o--|| FiscalYear : "classified into"
    Dividend }o--|| FiscalYear : "classified into"
    Distribution }o--|| FiscalYear : "classified into"
    CostBaseAdjustment }o--|| FiscalYear : "classified into"

    Account {
        string description
        currency base_currency
    }
    Market {
        string code
        string suffix
    }
    Instrument {
        string name
        currency currency
        decimal current_unit_price
    }
    Buy {
        date date
        decimal quantity
        money unit_price
        money total_brokerage
    }
    Sell {
        date date
        decimal quantity
        money unit_price
        string strategy
    }
    Parcel {
        decimal parcel_quantity
        decimal cumulative_split_multiplier
        date activation_date
        date deactivation_date
    }
    SellAllocation {
        decimal quantity
    }
    Dividend {
        date date
        money franked_amount_per_share
        money unfranked_amount_per_share
    }
    Distribution {
        date date
        money distribution_amount_per_share
    }
    CostBaseAdjustment {
        date financial_year_end_date
        money cost_base_increase
    }
    CostBaseAdjustmentAllocation {
        money cost_base_increase
    }
    ShareSplit {
        date date
        decimal quantity_before
        decimal quantity_after
    }
    ExchangeRate {
        date date
        currency convert_from
        currency convert_to
        decimal exchange_rate_multiplier
    }
    FiscalYear {
        int start_year
        string name
    }
    FiscalYearType {
        string description
        int start_month
        int start_day
    }
    InstrumentPriceHistory {
        date date
        decimal close
    }
```

*Not shown: AppUser, LogEntry, CurrentExchangeRate, DataExport.*
