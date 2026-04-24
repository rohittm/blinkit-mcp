import re

from .base import BaseService


class CheckoutService(BaseService):
    def _extract_amounts(self, text: str) -> list[float]:
        matches = re.findall(r"(?:₹|Rs\.?\s*)(\d+(?:,\d+)*(?:\.\d+)?)", text or "")
        return [float(match.replace(",", "")) for match in matches]

    async def _is_payment_option_disabled(self, panel):
        if await panel.count() == 0:
            return True

        panel_first = panel.first
        panel_text = (await panel_first.inner_text() or "").strip().lower()

        is_disabled_attr = await panel_first.get_attribute("disabled") is not None
        is_aria_disabled = (
            await panel_first.get_attribute("aria-disabled") == "true"
        )

        option_button = panel.locator("div[role='button'], button")
        if await option_button.count() > 0:
            button = option_button.first
            is_aria_disabled = is_aria_disabled or (
                await button.get_attribute("aria-disabled") == "true"
            )
            is_disabled_attr = is_disabled_attr or (
                await button.get_attribute("disabled") is not None
            )

        has_insufficient_marker = "insufficient" in panel_text
        return is_disabled_attr or is_aria_disabled or has_insufficient_marker

    async def _click_payment_option(self, panel):
        option_button = panel.locator("div[role='button'], button")
        if await option_button.count() > 0:
            await option_button.first.click()
        else:
            await panel.first.click()

    async def _select_blinkit_money_if_sufficient(self, frame):
        blinkit_money_panel = frame.locator(
            "div[title='Blinkit Money'], div:has-text('Blinkit Money')"
        )
        if await blinkit_money_panel.count() == 0:
            print("Blinkit Money option not found.")
            return None

        panel = blinkit_money_panel.first
        panel_text = (await panel.inner_text() or "").strip()

        if await self._is_payment_option_disabled(blinkit_money_panel):
            print("Blinkit Money is unavailable or insufficient for this order.")
            return None

        lowered_text = panel_text.lower()
        amounts = self._extract_amounts(panel_text)

        # Prefer explicit insufficiency markers. If amounts are present, require the
        # available balance to cover the amount due when the widget shows both.
        if "insufficient" in lowered_text:
            print("Blinkit Money balance is marked insufficient.")
            return None

        if len(amounts) >= 2 and amounts[0] < amounts[1]:
            print(
                "Blinkit Money balance does not appear to cover the order total."
            )
            return None

        print("Blinkit Money is available. Selecting it...")
        await self._click_payment_option(blinkit_money_panel)
        return "Selected Blinkit Money. Call pay_now to finalize."

    async def place_order(self):
        """Proceeds to checkout."""

        if await self._is_store_closed():
            return "CRITICAL: Store is closed."

        try:
            proceed_btn = (
                self.page.locator("button, div").filter(has_text="Proceed").last
            )

            # If Proceed not visible, try opening the cart first
            if not await proceed_btn.is_visible():
                print("Proceed button not visible. Attempting to open Cart drawer...")
                cart_btn = self.page.locator(
                    "div[class*='CartButton__Button'], div[class*='CartButton__Container']"
                )
                if await cart_btn.count() > 0:
                    await cart_btn.first.click()
                    print("Clicked 'My Cart' button.")
                    await self.page.wait_for_timeout(2000)
                else:
                    print("Could not find 'My Cart' button.")

            # Try clicking Proceed again
            if await proceed_btn.is_visible():
                await proceed_btn.click()
                print(
                    "Cart checkout successfully.\nYou can select the payment method and proceed to pay."
                )
                await self.page.wait_for_timeout(3000)
            else:
                print(
                    "Proceed button not visible. Cart might be empty or Store Unavailable."
                )

        except Exception as e:
            print(f"Error placing order: {e}")

    async def select_payment_method(self):
        """Select Blinkit Money first, then Cash, then fall back to UPI QR."""
        print("Selecting payment method (Blinkit Money -> Cash -> UPI QR)...")
        try:
            iframe_element = await self.page.wait_for_selector(
                "#payment_widget", timeout=30000
            )
            if not iframe_element:
                print("Payment widget iframe not found.")
                return "Payment widget not found."

            frame = await iframe_element.content_frame()
            if not frame:
                return "Payment widget frame content not found."

            await frame.wait_for_load_state("networkidle")

            blinkit_money_result = await self._select_blinkit_money_if_sufficient(frame)
            if blinkit_money_result:
                return blinkit_money_result

            # Check Cash
            cash_panel = frame.locator("div[title='Cash']")
            if await cash_panel.count() > 0:
                if not await self._is_payment_option_disabled(cash_panel):
                    print("Cash is available. Selecting Cash on Delivery...")
                    await self._click_payment_option(cash_panel)
                    return "Selected Cash on Delivery. Call pay_now to finalize."
                else:
                    print("Cash is unavailable or disabled.")
            else:
                print("Cash option not found.")

            # If Cash is disabled or doesn't exist, try UPI -> Generate QR
            print("Selecting UPI and generating QR code...")
            upi_panel = frame.locator("div[title='UPI']")
            if await upi_panel.count() > 0:
                upi_button = upi_panel.locator("div[role='button']")
                if await upi_button.count() > 0:
                    # Check if already open
                    is_open = (
                        await upi_button.first.get_attribute("aria-expanded") == "true"
                    )
                    if not is_open:
                        await upi_button.first.click()
                        print("Clicked UPI.")
                else:
                    await upi_panel.first.click()

                await self.page.wait_for_timeout(1000)

                # Click Generate QR
                generate_qr_btn = frame.locator("button:has-text('Generate QR')")
                if await generate_qr_btn.count() > 0:
                    await generate_qr_btn.first.click()
                    print("Generated QR code. Please show the QR code to the customer.")
                    await self.page.wait_for_timeout(2000)  # Wait for QR to load

                    try:
                        qr_img_locator = frame.locator(
                            "div[class*='QrImageWrapper'] img"
                        )
                        await qr_img_locator.wait_for(state="visible", timeout=5000)
                        qr_src = await qr_img_locator.first.get_attribute("src")
                        if qr_src and qr_src.startswith("data:image/"):
                            base64_data = qr_src.split(",")[1]
                            return {
                                "status": "UPI QR Code generated successfully. Show it to the customer.",
                                "qr_base64": base64_data,
                                "format": qr_src.split(";")[0].split("/")[1],
                            }
                    except Exception as qr_e:
                        print(f"Failed to extract QR Code image: {qr_e}")

                    return (
                        "UPI QR Code generated successfully. Show it to the customer."
                    )
                else:
                    print("Generate QR button not found within UPI.")
                    return "UPI section opened but 'Generate QR' button not found."
            else:
                print("UPI option not found.")
                return "UPI option not found in payment widget."

        except Exception as e:
            print(f"Error selecting payment method: {e}")
            return f"Error: {str(e)}"

    async def click_pay_now(self):
        """Clicks the final Pay Now button."""
        try:
            # Strategy 1: Specific class partial match
            pay_btn_specific = self.page.locator(
                "div[class*='Zpayments__Button']:has-text('Pay Now')"
            )
            if (
                await pay_btn_specific.count() > 0
                and await pay_btn_specific.first.is_visible()
            ):
                await pay_btn_specific.first.click()
                print("Clicked 'Pay Now'. Please approve the payment on your UPI app.")
                return "Clicked Pay Now."

            # Strategy 2: Text match on page
            pay_btn_text = (
                self.page.locator("div, button").filter(has_text="Pay Now").last
            )
            if await pay_btn_text.count() > 0 and await pay_btn_text.is_visible():
                await pay_btn_text.click()
                print("Clicked 'Pay Now'.")
                return "Clicked Pay Now."

            # Strategy 3: Check inside iframe
            iframe_element = await self.page.query_selector("#payment_widget")
            if iframe_element:
                frame = await iframe_element.content_frame()
                if frame:
                    frame_btn = frame.locator("text='Pay Now', text='Place Order'")
                    if await frame_btn.count() > 0:
                        await frame_btn.first.click()
                        print("Clicked payment button inside iframe.")
                        return "Clicked payment button inside iframe."

            print("Could not find 'Pay Now' button (timeout or not in DOM).")
            return "Could not find Pay Now button."

        except Exception as e:
            print(f"Error clicking Pay Now: {e}")
            return f"Error: {str(e)}"
