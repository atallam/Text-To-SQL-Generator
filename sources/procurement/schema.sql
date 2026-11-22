-- Synthetic direct-materials procurement schema (demo). Models the PO lifecycle: suppliers and parts,
-- purchase orders and lines, PO changes (pushouts), ASN shipments, receipts, inventory and RMAs.
DROP TABLE IF EXISTS rmas, receipts, shipment_lines, shipments, po_changes, po_lines, purchase_orders,
    inventory, supplier_parts, parts, suppliers CASCADE;

CREATE TABLE suppliers (
    id                  integer PRIMARY KEY,
    name                text NOT NULL,
    country             text NOT NULL,
    region              text NOT NULL CHECK (region IN ('APAC','EMEA','AMER')),
    tier                text NOT NULL CHECK (tier IN ('strategic','preferred','approved')),
    payment_terms_days  integer NOT NULL,
    onboarded_on        date NOT NULL,
    is_active           boolean NOT NULL
);

CREATE TABLE parts (
    id               integer PRIMARY KEY,
    part_number      text NOT NULL UNIQUE,
    description      text NOT NULL,
    category         text NOT NULL CHECK (category IN
                       ('memory','storage','display','battery','pcb','chassis','cable','packaging')),
    uom              text NOT NULL,
    standard_cost    numeric(12,4) NOT NULL,
    moq              integer NOT NULL,          -- default minimum order quantity
    lead_time_days   integer NOT NULL           -- default (planned) lead time
);

CREATE TABLE supplier_parts (
    supplier_id      integer NOT NULL REFERENCES suppliers(id),
    part_id          integer NOT NULL REFERENCES parts(id),
    unit_price       numeric(12,4) NOT NULL,
    lead_time_days   integer NOT NULL,
    moq              integer NOT NULL,
    is_primary       boolean NOT NULL,
    PRIMARY KEY (supplier_id, part_id)
);

CREATE TABLE purchase_orders (
    id           integer PRIMARY KEY,
    po_number    text NOT NULL UNIQUE,
    supplier_id  integer NOT NULL REFERENCES suppliers(id),
    buyer        text NOT NULL,
    status       text NOT NULL CHECK (status IN ('open','partially_received','closed','cancelled')),
    order_date   date NOT NULL,
    currency     char(3) NOT NULL
);
CREATE INDEX idx_po_supplier ON purchase_orders (supplier_id);
CREATE INDEX idx_po_order_date ON purchase_orders (order_date);

CREATE TABLE po_lines (
    id              integer PRIMARY KEY,
    po_id           integer NOT NULL REFERENCES purchase_orders(id),
    line_no         integer NOT NULL,
    part_id         integer NOT NULL REFERENCES parts(id),
    quantity        integer NOT NULL CHECK (quantity > 0),
    unit_price      numeric(12,4) NOT NULL,
    requested_date  date NOT NULL,              -- date the buyer originally asked for
    promised_date   date NOT NULL,              -- current date committed by the supplier
    status          text NOT NULL CHECK (status IN ('open','partially_received','received','cancelled')),
    UNIQUE (po_id, line_no)
);
CREATE INDEX idx_po_lines_po ON po_lines (po_id);
CREATE INDEX idx_po_lines_part ON po_lines (part_id);
CREATE INDEX idx_po_lines_promised ON po_lines (promised_date);

CREATE TABLE po_changes (
    id            integer PRIMARY KEY,
    po_line_id    integer NOT NULL REFERENCES po_lines(id),
    change_type   text NOT NULL CHECK (change_type IN ('pushout','pullin','quantity_change','cancellation')),
    old_date      date,
    new_date      date,
    old_quantity  integer,
    new_quantity  integer,
    reason        text NOT NULL CHECK (reason IN
                    ('supplier_capacity','demand_change','material_shortage','logistics','quality_hold')),
    initiated_by  text NOT NULL CHECK (initiated_by IN ('buyer','supplier')),
    changed_on    date NOT NULL
);
CREATE INDEX idx_po_changes_line ON po_changes (po_line_id);
CREATE INDEX idx_po_changes_date ON po_changes (changed_on);

CREATE TABLE shipments (
    id           integer PRIMARY KEY,
    po_id        integer NOT NULL REFERENCES purchase_orders(id),
    asn_number   text NOT NULL UNIQUE,           -- advance ship notice
    carrier      text NOT NULL,
    ship_date    date NOT NULL,
    eta_date     date NOT NULL,
    status       text NOT NULL CHECK (status IN ('in_transit','delivered','delayed'))
);
CREATE INDEX idx_shipments_po ON shipments (po_id);

CREATE TABLE shipment_lines (
    id            integer PRIMARY KEY,
    shipment_id   integer NOT NULL REFERENCES shipments(id),
    po_line_id    integer NOT NULL REFERENCES po_lines(id),
    quantity      integer NOT NULL CHECK (quantity > 0)
);
CREATE INDEX idx_shipment_lines_shipment ON shipment_lines (shipment_id);

CREATE TABLE receipts (
    id                 integer PRIMARY KEY,
    po_line_id         integer NOT NULL REFERENCES po_lines(id),
    received_on        date NOT NULL,
    quantity_received  integer NOT NULL CHECK (quantity_received >= 0),
    quantity_rejected  integer NOT NULL CHECK (quantity_rejected >= 0)
);
CREATE INDEX idx_receipts_line ON receipts (po_line_id);
CREATE INDEX idx_receipts_date ON receipts (received_on);

CREATE TABLE inventory (
    id             integer PRIMARY KEY,
    part_id        integer NOT NULL REFERENCES parts(id),
    site           text NOT NULL,
    on_hand        integer NOT NULL,
    safety_stock   integer NOT NULL,
    reorder_point  integer NOT NULL,
    UNIQUE (part_id, site)
);

CREATE TABLE rmas (
    id          integer PRIMARY KEY,
    po_line_id  integer NOT NULL REFERENCES po_lines(id),
    supplier_id integer NOT NULL REFERENCES suppliers(id),
    part_id     integer NOT NULL REFERENCES parts(id),
    rma_date    date NOT NULL,
    quantity    integer NOT NULL CHECK (quantity > 0),
    reason      text NOT NULL CHECK (reason IN ('defective','wrong_part','damaged_in_transit','late_failure')),
    status      text NOT NULL CHECK (status IN ('open','shipped_back','credited','closed'))
);
CREATE INDEX idx_rmas_supplier ON rmas (supplier_id);
CREATE INDEX idx_rmas_date ON rmas (rma_date);

-- Descriptions: used by the schema-relevance filter, so questions find the right tables.
COMMENT ON TABLE suppliers IS 'Supplier master: one row per supplier with country, region, tier and payment terms.';
COMMENT ON TABLE parts IS 'Part master for direct materials: part number, category, standard cost, default MOQ and planned lead time.';
COMMENT ON TABLE supplier_parts IS 'Catalog of which supplier can supply which part, with that supplier price, lead time and MOQ. Not used to attribute POs, receipts or pushouts to a supplier.';
COMMENT ON TABLE purchase_orders IS 'PO header: one row per purchase order. Links a PO to its supplier (supplier_id), buyer, order date and status.';
COMMENT ON TABLE po_lines IS 'PO line: one row per part on a PO with quantity, unit price, requested_date (original ask) and promised_date (current supplier commitment).';
COMMENT ON TABLE po_changes IS 'Change history of PO lines: pushouts and pull-ins (date moves), quantity changes and cancellations, with reason and who initiated them.';
COMMENT ON TABLE shipments IS 'Advance ship notice (ASN) per PO shipment: carrier, ship date, ETA and shipment status.';
COMMENT ON TABLE shipment_lines IS 'Quantity of each PO line included in a shipment (ASN).';
COMMENT ON TABLE receipts IS 'Goods receipts per PO line: quantity received and quantity rejected on the receipt date.';
COMMENT ON TABLE inventory IS 'Current stock per part and site (Hyderabad, Austin, Penang): on-hand quantity, safety stock and reorder point.';
COMMENT ON TABLE rmas IS 'Return material authorizations: defective, wrong or damaged parts sent back to a supplier.';
COMMENT ON COLUMN inventory.on_hand IS 'Units currently in stock at the site.';
COMMENT ON COLUMN po_lines.requested_date IS 'Date the buyer originally asked for.';
COMMENT ON COLUMN po_lines.promised_date IS 'Current delivery date committed by the supplier, after any pushouts.';
COMMENT ON COLUMN po_changes.change_type IS 'pushout = date moved later, pullin = date moved earlier.';
COMMENT ON COLUMN receipts.quantity_rejected IS 'Units rejected on receipt (part of quantity_received).';
