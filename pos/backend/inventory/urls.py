from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import VendorViewSet, ProductViewSet, PurchaseOrderViewSet, ProductLotViewSet, StockTransactionViewSet, StockTransferViewSet, StockRequestViewSet, LowStockAlertView, TransferBillViewSet

router = DefaultRouter()
router.register(r'vendors', VendorViewSet, basename='vendor')
router.register(r'products', ProductViewSet, basename='product')
router.register(r'purchase-orders', PurchaseOrderViewSet, basename='purchase-order')
router.register(r'lots', ProductLotViewSet, basename='lot')
router.register(r'stock-transactions', StockTransactionViewSet, basename='stock-transaction')
router.register(r'stock_transfers', StockTransferViewSet, basename='stock-transfer')
router.register(r'stock_requests', StockRequestViewSet, basename='stock-request')
router.register(r'transfer_bills', TransferBillViewSet, basename='transfer-bill')

urlpatterns = [
    path('api/low_stock/', LowStockAlertView.as_view(), name='low_stock'),
    path('api/', include(router.urls)),
]
