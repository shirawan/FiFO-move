def transfer_purchase_ownership(cr):
    cr.execute("""UPDATE ir_model_constraint SET module = (SELECT id FROM ir_module_module WHERE name = 'company_purchase_cutover')
        WHERE module = (SELECT id FROM ir_module_module WHERE name = 'company_financial_cutover')
        AND name IN ('company_financial_purchase_history_unique_order', 'purchase_order_unique_history')""")
    cr.execute("""UPDATE ir_model_data SET module = 'company_purchase_cutover'
        WHERE module = 'company_financial_cutover' AND (
            name LIKE 'model_company_financial_purchase_%'
            OR name LIKE 'field_company_financial_purchase_%'
            OR name = 'field_purchase_order__financial_purchase_history_id'
            OR name = 'field_company_financial_cutover__purchase_history_ids'
            OR name LIKE 'purchase_history_%' OR name = 'purchase_vendor_choice_form'
            OR name LIKE 'access_purchase_%'
            OR name LIKE 'selection__company_financial_purchase_%'
            OR name IN ('constraint_company_financial_purchase_history_unique_order', 'constraint_purchase_order_unique_history'))""")
